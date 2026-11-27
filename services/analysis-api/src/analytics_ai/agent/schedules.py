"""Weekly/monthly calendar scheduling with atomic occurrence claims."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from analytics_ai.agent.models import AgentRecord, ReportSchedule
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import record_id
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import (
    AnalysisSession,
    AnalysisState,
    DatePeriod,
    utc_now,
)
from analytics_ai.domain.scope import source_scope_fingerprint
from analytics_ai.orchestrator.service import AnalysisOrchestrationError


def next_run(schedule: ReportSchedule, after: datetime) -> datetime:
    zone = ZoneInfo(schedule.timezone)
    local = after.astimezone(zone)
    # Days 1-28 avoid missing-month surprises; nonexistent DST hours are skipped.
    for offset in range(370):
        day = local.date() + timedelta(days=offset)
        if schedule.frequency == "weekly" and day.weekday() != schedule.weekday:
            continue
        if schedule.frequency == "monthly" and day.day != schedule.monthday:
            continue
        candidate = datetime(day.year, day.month, day.day, schedule.hour, tzinfo=zone)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(zone).hour != schedule.hour:
            continue
        if instant > after:
            return instant
    raise ValueError("schedule_next_run_unavailable")


def reporting_period(schedule: ReportSchedule, occurrence: datetime) -> DatePeriod:
    day = occurrence.astimezone(ZoneInfo(schedule.timezone)).date()
    if schedule.frequency == "weekly":
        end = day - timedelta(days=day.weekday() + 1)
        return DatePeriod(start=end - timedelta(days=6), end=end)
    end = day.replace(day=1) - timedelta(days=1)
    return DatePeriod(start=end.replace(day=1), end=end)


class ScheduleService:
    def __init__(self, agent: AgentService) -> None:
        self.agent = agent

    async def list(self, user: str) -> list[ReportSchedule]:
        await self.agent.preferences(user)
        return [
            ReportSchedule.model_validate(r.payload)
            for r in await self.agent.store.list_owner("schedule", user)
        ]

    async def save(
        self, user: str, value: ReportSchedule, *, now: datetime | None = None
    ) -> ReportSchedule:
        await self.agent.preferences(user)
        if value.enabled and (
            self.agent.director is None or self.agent.orchestrator is None
        ):
            raise AnalysisOrchestrationError("analytics_tools_disabled")
        schedule_id = value.id or uuid.uuid4().hex
        if not value.id and value.revision != 0:
            raise AnalysisOrchestrationError("agent_revision_conflict")
        current = await self.agent.store.get("schedule", schedule_id)
        if value.id and (
            current is None
            or current.owner_id != user
            or current.data_source_set_id != value.data_source_set_id
        ):
            raise AccessDenied("schedule_owner_mismatch")
        previous = ReportSchedule.model_validate(current.payload) if current else None
        # Owners can stop their own schedule even after its grant is revoked.
        if value.enabled or previous is None:
            scope = await self.agent.authorization.authorize_source_set(
                user, value.data_source_set_id
            )
            if scope.source_set.ga4 is None:
                raise AccessDenied("schedule_requires_ga4")
        updated = value.model_copy(
            update={
                "id": schedule_id,
                "revision": value.revision + 1,
                "next_run_at": next_run(value, now or utc_now())
                if value.enabled
                else None,
                "last_session_id": previous.last_session_id if previous else None,
                "last_status": previous.last_status if previous else None,
                "last_error": previous.last_error if previous else None,
                "running_until": previous.running_until if previous else None,
            }
        )
        await self.agent.audit(
            user,
            value.data_source_set_id,
            "agent.schedule.update",
            "attempt",
            schedule_id=schedule_id,
        )
        if not await self.agent.store.save(self.record(user, updated), value.revision):
            raise AnalysisOrchestrationError("agent_revision_conflict")
        return updated

    @staticmethod
    def record(user: str, value: ReportSchedule) -> AgentRecord:
        return AgentRecord(
            id=value.id,
            kind="schedule",
            owner_id=user,
            data_source_set_id=value.data_source_set_id,
            revision=value.revision,
            payload=value.model_dump(mode="json"),
            due_at=value.running_until
            or (value.next_run_at if value.enabled else None),
        )

    async def run_due(self, now: datetime | None = None) -> int:
        if self.agent.director is None or self.agent.orchestrator is None:
            raise AnalysisOrchestrationError("analytics_tools_disabled")
        fixed_now = now
        now = now or utc_now()
        count = 0
        for record in await self.agent.store.due(now):
            now = fixed_now or utc_now()
            schedule = ReportSchedule.model_validate(record.payload)
            if schedule.running_until:
                if schedule.running_until > now:
                    continue
                interrupted = schedule.model_copy(
                    update={
                        "revision": schedule.revision + 1,
                        "running_until": None,
                        "last_status": "failed",
                        "last_error": "worker_interrupted_no_automatic_replay",
                    }
                )
                if await self.agent.store.save(
                    self.record(record.owner_id, interrupted), schedule.revision
                ):
                    await self.agent.audit(
                        record.owner_id,
                        schedule.data_source_set_id,
                        "agent.schedule.run",
                        "interrupted",
                        schedule_id=schedule.id,
                    )
                continue
            if not schedule.enabled or not schedule.next_run_at:
                continue
            occurrence = schedule.next_run_at
            session_id = (
                "session_" + record_id(schedule.id, occurrence.isoformat())[:32]
            )
            claimed = schedule.model_copy(
                update={
                    "revision": schedule.revision + 1,
                    "next_run_at": next_run(schedule, now),
                    "last_status": "running",
                    "last_session_id": session_id,
                    "last_error": None,
                    "running_until": now + timedelta(seconds=360),
                }
            )
            if not await self.agent.store.save(
                self.record(record.owner_id, claimed), schedule.revision
            ):
                continue
            count += 1
            state = "failed"
            error_code: str | None = None
            try:
                # Use the owner's explicit delegation, never fabricate Firebase tokens.
                scope = await self.agent.authorization.authorize_source_set(
                    record.owner_id, schedule.data_source_set_id
                )
                latest = await self.agent.store.get("schedule", record.id)
                if latest is None or latest.revision != claimed.revision:
                    continue  # edited/paused after claiming: skip the old execution
                period = reporting_period(schedule, occurrence)
                session = AnalysisSession(
                    id=session_id,
                    user_id=record.owner_id,
                    company_id=scope.source_set.company_id,
                    data_source_set_id=scope.source_set.id,
                    data_access_profile_id=scope.source_set.data_access_profile_id,
                    source_scope_fingerprint=source_scope_fingerprint(scope.source_set),
                    title=f"{scope.source_set.name}｜{schedule.name} {period.start}"[
                        :120
                    ],
                    state=AnalysisState(
                        data_source_set_id=scope.source_set.id,
                        semantic_version=scope.source_set.semantic_version,
                        period=period,
                    ),
                )
                await self.agent.repository.create_session(session)
                async for event in self.agent.turn(
                    user_id=record.owner_id,
                    session_id=session_id,
                    content=schedule.prompt,
                    operation_id="schedule_"
                    + record_id(schedule.id, occurrence.isoformat())[:32],
                    background=True,
                    period=period,
                ):
                    if event["event"] == "done":
                        state = event["data"]["status"]
                    elif event["event"] == "error":
                        error_code = event["data"]["code"]
            except AccessDenied:
                error_code = "access_denied"
            except Exception:
                error_code = "scheduled_report_failed"
            latest = await self.agent.store.get("schedule", record.id)
            current = (
                ReportSchedule.model_validate(latest.payload) if latest else claimed
            )
            if current.last_session_id != claimed.last_session_id:
                continue
            finished = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "last_status": state,
                    "last_error": error_code,
                    "running_until": None,
                }
            )
            # A user's intervening edit must win over a worker's status write.
            await self.agent.store.save(
                self.record(record.owner_id, finished), current.revision
            )
            await self.agent.audit(
                record.owner_id,
                schedule.data_source_set_id,
                "agent.schedule.run",
                state,
                schedule_id=schedule.id,
                session_id=session_id,
                error=error_code,
            )
        return count
