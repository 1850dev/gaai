"""Persistent plan/observe/refine loop using the existing authorized executor."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from datetime import timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from analytics_ai.agent.browser_director import BrowserAgentDirector
from analytics_ai.agent.capabilities import capability_context, effective_definitions
from analytics_ai.agent.continuation_contract import (
    ContinuationContract,
    condition_change_continuation,
    filter_continuation,
)
from analytics_ai.agent.conversation import (
    existing_data_only,
    existing_result_discussion,
    explicit_filter_requirements,
    planning_workspace,
    query_context,
    restore_query_context,
    reusable_sections,
)
from analytics_ai.agent.director import (
    AgentDirector,
    PlannerPhaseTimeout,
    PlannerPhaseValidationError,
    validation_feedback,
)
from analytics_ai.agent.explicit_comparison import explicit_comparison_table
from analytics_ai.agent.facts import finding_context
from analytics_ai.agent.grounding import GroundingRejected
from analytics_ai.agent.markdown import bounded_document, cell, prose, text
from analytics_ai.agent.memory import ConversationMemory, remember_user
from analytics_ai.agent.memory_models import (
    AgreementChange,
    MemoryEntry,
    WorkingMemoryChange,
)
from analytics_ai.agent.models import (
    AgentDecision,
    AgentExecutionStep,
    AgentRecord,
    AgentSummary,
    AgentWorkspace,
    PropertyPreferences,
    ReportSection,
    SiteContext,
)
from analytics_ai.agent.operations import Operation, OperationLedger, request_hash
from analytics_ai.agent.presentation import error_label, render_summary, report_intent
from analytics_ai.agent.purpose import prior_goal_context
from analytics_ai.agent.query_repair import RECOVERABLE_GA4_ERRORS, QueryRepair
from analytics_ai.agent.research import (
    ReviewAgenda,
    explicit_half_year_review,
    is_multifaceted_review,
    query_signature,
)
from analytics_ai.agent.site_context import site_understanding
from analytics_ai.agent.staged import (
    create_staged,
    explicit_grouped_request,
    is_resume,
    next_query,
    record_result,
    render_progress,
)
from analytics_ai.agent.staged_history import RecoveredStaged, recover_staged
from analytics_ai.agent.staged_models import StagedPlan
from analytics_ai.agent.store import AgentStore, record_id
from analytics_ai.agent.workflows import (
    explicit_dimension_fields,
    explicit_semantic_fields,
    joint_dimension_fields,
)
from analytics_ai.analysis_state.reducer import AnalysisStateReducer
from analytics_ai.authorization.service import AccessDenied, AuthorizationService
from analytics_ai.domain.models import (
    AnalysisFilter,
    AnalysisMessage,
    AnalysisResultReference,
    AnalysisSession,
    AnalysisState,
    AuditEvent,
    DatePeriod,
    Ga4AuthMode,
    MessageRole,
    Status,
    utc_now,
)
from analytics_ai.ga4.table_order import TIME_TABLE_FIELDS, is_channel_table_field
from analytics_ai.orchestrator.category_comparison import category_values
from analytics_ai.orchestrator.planning import (
    ComparisonIntent,
    PlanCompiler,
    VertexIntentPlanner,
    explicit_month_comparison,
)
from analytics_ai.orchestrator.service import (
    AnalysisOrchestrationError,
    AnalysisOrchestrator,
)
from analytics_ai.orchestrator.skills import DEFAULT_SKILL_REGISTRY, AnalysisSkillId
from analytics_ai.orchestrator.time_grain import time_grain_request
from analytics_ai.repositories.protocols import ApplicationRepository
from analytics_ai.semantic.observed import observed_definition, observed_name
from analytics_ai.visualization.planner import DIMENSION_LABELS, METRIC_LABELS


class AgentService:
    def __init__(
        self,
        repository: ApplicationRepository,
        store: AgentStore,
        director: AgentDirector | None,
        orchestrator: AnalysisOrchestrator | None,
        vertex_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.store = store
        self.director = director
        self.orchestrator = orchestrator
        self.vertex_allowed = vertex_allowed
        self.authorization = AuthorizationService(repository)
        self.memory = ConversationMemory(repository, store)
        self.operations = OperationLedger(store)

    @staticmethod
    def missing_fields(workspace: AgentWorkspace) -> tuple[str, ...]:
        """Completion is evidence-based and cannot be bypassed by workflow ID.

        New analysis needs fresh sections. A discussion of an existing result may
        use only server-selected, reauthorized, exact-condition references.
        """
        sections = [
            s
            for s in workspace.sections
            if (
                s.operation_id == workspace.operation_id
                or s.message_id in workspace.reused_section_ids
            )
            and s.evidence_ids
            and s.observations
            and (
                workspace.required_filters is None
                or set(s.filters) == set(workspace.required_filters)
            )
            and (
                workspace.required_period is None
                or s.period == workspace.required_period
            )
            and (
                not workspace.comparison_required_exactly
                or s.comparison_period == workspace.required_comparison_period
            )
            and (
                workspace.required_comparison_period is None
                or (
                    s.comparison_period == workspace.required_comparison_period
                    and {"current", "comparison"}.issubset(s.observed_periods)
                )
            )
        ]
        dimensions = {field for s in sections for field in s.dimension_fields}
        metrics = {field for s in sections for field in s.metric_fields}
        missing = [
            *sorted(set(workspace.required_dimension_fields) - dimensions),
            *sorted(set(workspace.required_metric_fields) - metrics),
        ]
        for axis in workspace.required_dimension_fields:
            if axis not in dimensions:
                continue
            axis_metrics = {
                field
                for s in sections
                if axis in s.dimension_fields
                for field in s.metric_fields
            }
            missing.extend(
                f"{axis} × {metric}"
                for metric in workspace.required_metric_fields
                if metric in metrics and metric not in axis_metrics
            )
        for group in workspace.required_dimension_groups:
            joint_sections = [
                s
                for s in sections
                if set(group).issubset(s.dimension_fields)
                and any(
                    all(row.get(field) is not None for field in group)
                    for row in s.observations
                )
            ]
            if not joint_sections:
                missing.append(" × ".join(group))
            else:
                for metric in workspace.required_metric_fields:
                    if not any(
                        metric in s.metric_fields
                        and any(
                            all(
                                row.get(field) is not None for field in (*group, metric)
                            )
                            for row in s.observations
                        )
                        for s in joint_sections
                    ):
                        missing.append(" × ".join((*group, metric)))
        if workspace.required_comparison_period and not sections:
            missing.append("指定した両期間の比較")
        if workspace.required_filters is not None and not sections:
            missing.append("指定した絞込み条件")
        return tuple(missing)

    async def audit(
        self, user: str, source: str, action: str, status: str, **details: Any
    ) -> None:
        await self.repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=str(details.pop("request_id", "agent")),
                user_id=user,
                data_source_set_id=source or None,
                action=action,
                status=status,
                details=details,
            )
        )

    async def site(self, user: str, source: str) -> SiteContext:
        await self.authorization.authorize_source_set(user, source)
        record = await self.store.get("site", record_id(user, source))
        return SiteContext.model_validate(record.payload) if record else SiteContext()

    async def save_site(self, user: str, source: str, site: SiteContext) -> SiteContext:
        await self.authorization.authorize_source_set(user, source)
        result = site.model_copy(
            update={"revision": site.revision + 1, "updated_at": utc_now()}
        )
        await self.audit(
            user, source, "agent.site.update", "attempt", revision=result.revision
        )
        if not await self.store.save(
            AgentRecord(
                id=record_id(user, source),
                kind="site",
                owner_id=user,
                data_source_set_id=source,
                revision=result.revision,
                payload=result.model_dump(mode="json"),
            ),
            site.revision,
        ):
            raise AnalysisOrchestrationError("agent_revision_conflict")
        return result

    async def preferences(self, user: str) -> PropertyPreferences:
        account = await self.repository.get_user(user)
        if account is None or account.status is not Status.ACTIVE:
            raise AccessDenied("inactive_or_unregistered_user")
        record = await self.store.get("preferences", record_id(user))
        return (
            PropertyPreferences.model_validate(record.payload)
            if record
            else PropertyPreferences()
        )

    async def save_preferences(
        self, user: str, preferences: PropertyPreferences
    ) -> PropertyPreferences:
        await self.preferences(user)
        result = preferences.model_copy(update={"revision": preferences.revision + 1})
        await self.audit(user, "", "agent.preferences.update", "attempt")
        if not await self.store.save(
            AgentRecord(
                id=record_id(user),
                kind="preferences",
                owner_id=user,
                revision=result.revision,
                payload=result.model_dump(mode="json"),
            ),
            preferences.revision,
        ):
            raise AnalysisOrchestrationError("agent_revision_conflict")
        return result

    async def workspace(self, user: str, session_id: str) -> AgentWorkspace:
        session = await self.session(user, session_id)
        record = await self.store.get("workspace", record_id(user, session.id))
        if record:
            self.memory.check_record(record, user, session.data_source_set_id)
        return (
            AgentWorkspace.model_validate(record.payload)
            if record
            else AgentWorkspace()
        )

    async def session(self, user: str, session_id: str) -> AnalysisSession:
        session = await self.repository.get_session(session_id)
        if session is None:
            raise AccessDenied("session_not_found")
        await self.authorization.authorize_session(user, session)
        if session.state.data_source_set_id != session.data_source_set_id:
            raise AccessDenied("agent_state_scope_mismatch")
        return session

    async def validate_staged(
        self, session: AnalysisSession, staged: StagedPlan
    ) -> None:
        scope = await self.authorization.authorize_session(session.user_id, session)
        if staged.source_fingerprint != record_id(str(scope.source_set.ga4)):
            raise AccessDenied("staged_source_changed")
        ids = {
            *staged.rank_evidence_ids,
            *(eid for item in staged.items for eid in item.evidence_ids),
        }
        for eid in ids:
            evidence = await self.repository.get_evidence(eid)
            if (
                not evidence
                or evidence.company_id != session.company_id
                or evidence.data_source_set_id != session.data_source_set_id
                or not scope.source_set.ga4
                or evidence.ga4_property_id != scope.source_set.ga4.property_id
            ):
                raise AccessDenied("staged_evidence_unavailable")

    async def change_working_memory(
        self, user: str, session_id: str, change: WorkingMemoryChange
    ) -> AgentWorkspace:
        session = await self.session(user, session_id)
        workspace = await self.workspace(user, session_id)
        if workspace.revision != change.revision:
            raise AnalysisOrchestrationError("agent_revision_conflict")
        if (
            workspace.status == "running"
            and workspace.lease_until
            and workspace.lease_until > utc_now()
        ):
            raise AnalysisOrchestrationError("agent_turn_in_progress")
        entries = [
            e
            for e in workspace.memory.entries
            if (e.kind, e.text) != (change.kind, change.text)
        ]
        if change.action == "add":
            if change.kind == "objective":
                entries = [e for e in entries if e.kind != "objective"]
            entries.append(
                MemoryEntry(
                    kind=change.kind, text=change.text, message_id="user_memory_edit"
                )
            )
        await self.audit(
            user,
            session.data_source_set_id,
            "memory.working." + change.action,
            "attempt",
            session_id=session_id,
            kind=change.kind,
        )
        return await self.checkpoint(
            session,
            workspace,
            memory=workspace.memory.model_copy(
                update={
                    "entries": tuple(entries[-40:]),
                    "trimmed": workspace.memory.trimmed or len(entries) > 40,
                }
            ),
        )

    async def checkpoint(
        self, session: AnalysisSession, workspace: AgentWorkspace, **changes: Any
    ) -> AgentWorkspace:
        prepare_only = changes.pop("prepare_only", False)
        updated = workspace.model_copy(
            update={
                **changes,
                "revision": workspace.revision + 1,
                "updated_at": utc_now(),
            }
        )
        if changes.get("status") in {"waiting", "complete", "partial"}:
            # Write an immutable candidate BEFORE committing the final answer.
            # Recovery releases it only if that answer exists in the repository.
            await self.operations.prepare(
                session.user_id, session.data_source_set_id, session.id, updated
            )
        if not prepare_only:
            await self._save_workspace(session, workspace.revision, updated)
        return updated

    async def _save_workspace(
        self, session: AnalysisSession, expected: int, updated: AgentWorkspace
    ) -> None:
        if not await self.store.save(
            AgentRecord(
                id=record_id(session.user_id, session.id),
                kind="workspace",
                owner_id=session.user_id,
                data_source_set_id=session.data_source_set_id,
                revision=updated.revision,
                payload=updated.model_dump(mode="json"),
            ),
            expected,
        ):
            raise AnalysisOrchestrationError("agent_revision_conflict")

    async def facts(
        self, session: AnalysisSession, workspace: AgentWorkspace
    ) -> list[dict[str, Any]]:
        current_sections = tuple(
            s
            for s in workspace.sections
            if s.operation_id == workspace.operation_id
            or s.message_id in workspace.reused_section_ids
        )
        relevant_sections = current_sections or workspace.sections
        ids = list(
            dict.fromkeys(
                ([] if current_sections else [ref.id for ref in session.state.findings])
                + [fid for section in relevant_sections for fid in section.finding_ids]
            )
        )[-32:]
        facts: list[dict[str, Any]] = []
        for fid in ids:
            finding = await self.repository.get_finding(fid)
            if (
                finding is None
                or finding.session_id != session.id
                or finding.user_id != session.user_id
                or finding.company_id != session.company_id
                or finding.data_source_set_id != session.data_source_set_id
            ):
                continue
            evidence_ids = []
            quality_notes: list[str] = []
            for eid in finding.evidence_ids:
                evidence = await self.repository.get_evidence(eid)
                if (
                    evidence
                    and evidence.data_source_set_id == session.data_source_set_id
                    and evidence.company_id == session.company_id
                    and evidence.dataset_id == finding.dataset_id
                ):
                    evidence_ids.append(eid)
                    quality_notes.extend(evidence.quality.notes)
            if evidence_ids:
                fact = finding_context(finding, evidence_ids)
                section = next(
                    (s for s in relevant_sections if fid in s.finding_ids), None
                )
                if section and not finding.dimension_fields:
                    # Read-only compatibility for already saved reports. Never
                    # rewrite history or pretend a missing cell was observed.
                    fact["dimension_fields"] = list(section.dimension_fields)
                    if not fact["category_values"]:
                        fact["category_values"] = [
                            cell.model_dump(mode="json")
                            for cell in category_values(
                                [
                                    row
                                    for row in section.observations
                                    if row.get("dateRange", "current") == "current"
                                ],
                                fields=section.dimension_fields,
                                metric=finding.metric_field,
                            )
                        ]
                fact.update(
                    {
                        "quality_notes": list(dict.fromkeys(quality_notes)),
                        "period": section.period.model_dump(mode="json")
                        if section and section.period
                        else None,
                        "comparison_period": section.comparison_period.model_dump(
                            mode="json"
                        )
                        if section and section.comparison_period
                        else None,
                        "perspective": section.perspective if section else None,
                        "filters": [f.model_dump(mode="json") for f in section.filters]
                        if section
                        else [],
                    }
                )
                facts.append(fact)
        return facts

    @staticmethod
    def validate_decision(
        decision: AgentDecision,
        site: SiteContext,
        facts: list[dict[str, Any]],
        metrics: set[str],
    ) -> None:
        # `site` remains in the call contract for compatibility. Site settings
        # describe the business, not permission to analyze or propose changes.
        # Evidence, registered metrics and read-only tool safety still apply.
        evidence = {eid for fact in facts for eid in fact["evidence_ids"]}
        for proposal in decision.proposals:
            if not set(proposal.evidence_ids).issubset(evidence) or (
                proposal.success_metric is not None
                and proposal.success_metric not in metrics
            ):
                raise ValueError("proposal_evidence_or_metric_invalid")
        if decision.plan and (
            decision.plan.sql
            or decision.plan.requires_raw_events
            or decision.plan.requires_complex_join
            or decision.plan.requires_text_ai
        ):
            raise ValueError("agent_ga4_only")

    async def operation(
        self, user_id: str, session_id: str, operation_id: str
    ) -> dict[str, Any]:
        session = await self.session(user_id, session_id)
        scope = await self.authorization.authorize_session(user_id, session)
        record = await self.operations.get(user_id, session_id, operation_id)
        if record is None:
            raise AnalysisOrchestrationError("operation_not_found")
        value = Operation.model_validate(record.payload)
        if (
            record.data_source_set_id != session.data_source_set_id
            or value.source_hash
            != hashlib.sha256(scope.source_set.model_dump_json().encode()).hexdigest()
            or value.semantic_version != session.state.semantic_version
        ):
            raise AccessDenied("operation_context_changed")
        assistant_id = "msg_" + record_id("answer", session_id, operation_id)[:32]
        answer = next(
            (
                m
                for m in await self.repository.list_messages(session_id)
                if m.id == assistant_id
            ),
            None,
        )
        if answer:
            for ref in answer.result_references:
                await self.session(user_id, ref.session_id)
        if value.workspace_json and answer and value.status != "complete":
            restored = AgentWorkspace.model_validate_json(value.workspace_json)
            workspace = await self.workspace(user_id, session_id)
            if workspace.operation_id == operation_id and workspace.status in {
                "running",
                "failed",
            }:
                await self.checkpoint(
                    session,
                    workspace,
                    **{
                        key: getattr(restored, key)
                        for key in AgentWorkspace.model_fields
                        if key not in {"revision", "updated_at"}
                    },
                )
            await self.audit(
                user_id,
                session.data_source_set_id,
                "agent.operation_recovered",
                "success",
                request_id=operation_id,
            )
            value = value.model_copy(
                update={"status": "complete", "lease_until": None, "error_code": None}
            )
            record = await self.operations.save(
                user_id, session.data_source_set_id, value, record
            )
        elif value.status in {"running", "prepared"} and (
            value.lease_until is None or value.lease_until <= utc_now()
        ):
            value = value.model_copy(
                update={
                    "status": "interrupted",
                    "lease_until": None,
                    "error_code": "agent_interrupted",
                }
            )
            await self.operations.save(
                user_id, session.data_source_set_id, value, record
            )
        return {
            "operation_id": operation_id,
            "status": value.status,
            "error_code": value.error_code,
            "result": AgentWorkspace.model_validate_json(
                value.workspace_json
            ).model_dump(mode="json")
            if value.status == "complete" and value.workspace_json
            else None,
        }

    async def turn(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        operation_id: str,
        background: bool = False,
        period: DatePeriod | None = None,
        browser_director: BrowserAgentDirector | None = None,
        browser_approval: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        session = await self.session(user_id, session_id)
        if (
            browser_director is None
            and self.vertex_allowed is not None
            and not self.vertex_allowed(user_id)
        ):
            raise AccessDenied("browser_inference_required")
        scope = await self.authorization.authorize_session(user_id, session)
        if browser_director is not None and (
            background
            or not scope.source_set.ga4
            or scope.source_set.ga4.auth_mode is not Ga4AuthMode.USER_OAUTH
        ):
            raise AccessDenied("browser_requires_ga4_oauth_set")
        digest = request_hash(
            content, background, period.model_dump(mode="json") if period else None
        )
        record = await self.operations.get(user_id, session_id, operation_id)
        if record:
            if Operation.model_validate(record.payload).request_hash != digest:
                raise AnalysisOrchestrationError("operation_payload_conflict")
            saved = await self.operation(user_id, session_id, operation_id)
            if saved["status"] == "complete":
                yield {"event": "done", "data": saved["result"]}
            else:
                yield {
                    "event": "error",
                    "data": {"code": saved["error_code"] or "agent_turn_in_progress"},
                }
            return
        workspace = await self.workspace(user_id, session_id)
        if (
            workspace.status == "running"
            and workspace.lease_until
            and workspace.lease_until > utc_now()
        ):
            raise AnalysisOrchestrationError("agent_turn_in_progress")
        if workspace.operation_id == operation_id:
            raise AnalysisOrchestrationError("legacy_operation_requires_new_id")
        value = Operation(
            session_id=session_id,
            operation_id=operation_id,
            request_hash=digest,
            source_hash=hashlib.sha256(
                scope.source_set.model_dump_json().encode()
            ).hexdigest(),
            semantic_version=session.state.semantic_version,
            status="running",
            lease_until=utc_now() + timedelta(seconds=400),
        )
        await self.operations.save(user_id, session.data_source_set_id, value, None)
        try:
            async for event in self._execute_turn(
                user_id=user_id,
                session_id=session_id,
                content=content,
                operation_id=operation_id,
                background=background,
                period=period,
                browser_director=browser_director,
                browser_approval=browser_approval,
            ):
                if event["event"] in {"done", "error"}:
                    current = await self.operations.get(
                        user_id, session_id, operation_id
                    )
                    assert current is not None
                    stored = Operation.model_validate(current.payload)
                    # Another recovery reader may already have sealed this result.
                    if stored.status != "complete":
                        if event["event"] == "error":
                            changes = {
                                "status": "failed",
                                "lease_until": None,
                                "error_code": event["data"]["code"],
                            }
                        else:
                            changes = {
                                "status": "complete",
                                "lease_until": None,
                                "workspace_json": AgentWorkspace.model_validate(
                                    event["data"]
                                ).model_dump_json(),
                            }
                        await self.operations.save(
                            user_id,
                            session.data_source_set_id,
                            stored.model_copy(update=changes),
                            current,
                        )
                yield event
        except (Exception, asyncio.CancelledError):
            current = await asyncio.shield(
                self.operations.get(user_id, session_id, operation_id)
            )
            if current:
                stored = Operation.model_validate(current.payload)
                if stored.status != "complete":
                    await asyncio.shield(
                        self.operations.save(
                            user_id,
                            session.data_source_set_id,
                            stored.model_copy(
                                update={
                                    "status": "interrupted",
                                    "lease_until": None,
                                    "error_code": "agent_interrupted",
                                }
                            ),
                            current,
                        )
                    )
            raise

    async def _execute_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        content: str,
        operation_id: str,
        background: bool = False,
        period: DatePeriod | None = None,
        browser_director: BrowserAgentDirector | None = None,
        browser_approval: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        director = browser_director or self.director
        if director is None or self.orchestrator is None:
            raise AnalysisOrchestrationError("analytics_tools_disabled")
        session = await self.session(user_id, session_id)
        workspace = await self.workspace(user_id, session_id)
        if (
            workspace.status == "running"
            and workspace.lease_until
            and workspace.lease_until > utc_now()
        ):
            raise AnalysisOrchestrationError("agent_turn_in_progress")
        if workspace.operation_id == operation_id:
            if workspace.status in {"failed", "running"}:
                yield {
                    "event": "error",
                    "data": {"code": workspace.last_error or "agent_interrupted"},
                }
                return
            yield {"event": "done", "data": workspace.model_dump(mode="json")}
            return
        site = await self.site(user_id, session.data_source_set_id)
        requested = report_intent(content)
        saved_data_only = existing_data_only(content) or (
            browser_director is not None
            and requested is True
            and bool(
                re.search(r"ここまで|これまで|保存済み|取得済み|今の結果", content)
            )
            and not re.search(r"調べて|取得して|追加調査して|新たに分析", content)
        )
        browser_advice = (
            browser_director is not None
            and saved_data_only
            and bool(
                re.search(
                    r"改善(?:案|仮説|提案|策)|施策|具体化|コンテンツ案|検証方法",
                    content,
                )
            )
        )
        grain_request = time_grain_request(content)
        recovered_staged: RecoveredStaged | None = None
        preferred_evidence_ids: tuple[str, ...] = ()
        preferred_finding_ids: tuple[str, ...] = ()
        if saved_data_only:
            prior_messages = await self.repository.list_messages(session.id)
            previous_answer = next(
                (
                    m
                    for m in reversed(prior_messages)
                    if m.role is MessageRole.ASSISTANT
                    and m.kind == "chat"
                    and m.evidence_ids
                    and (m.user_id, m.company_id, m.data_source_set_id)
                    == (user_id, session.company_id, session.data_source_set_id)
                ),
                None,
            )
            if previous_answer:
                preferred_evidence_ids = previous_answer.evidence_ids
                preferred_finding_ids = previous_answer.finding_ids
        resume = not background and not saved_data_only and is_resume(content)
        staged_active = bool(
            resume
            and workspace.staged_plan
            and workspace.staged_plan.phase != "complete"
        )
        if (
            staged_active
            and workspace.staged_plan
            and (
                workspace.staged_plan.anchor.data_source_set_id
                != session.data_source_set_id
                or workspace.staged_plan.anchor.semantic_version
                != session.state.semantic_version
            )
        ):
            raise AnalysisOrchestrationError("staged_plan_context_changed")
        if staged_active and workspace.staged_plan:
            await self.validate_staged(session, workspace.staged_plan)
        # Index existing authorized sections lazily as well as every new result.
        # This is metadata only: no GA4 query and no history rewriting.
        for old_section in workspace.sections:
            await self.memory.archive(session, old_section.title, old_section)
        retrieval = await self.memory.search(
            user_id,
            session.data_source_set_id,
            content
            + " "
            + " ".join(e.text for e in workspace.memory.entries if e.kind == "target"),
            session_id=session.id,
        )
        agreements = await self.memory.agreements(user_id, session.data_source_set_id)
        user_message_id = "msg_" + record_id("user", session.id, operation_id)[:32]
        working = remember_user(workspace.memory, content, user_message_id)
        # Restore relevant same-session observations without treating them as a
        # newly executed query. Other sessions stay labelled historical context.
        retrieved_sections = [
            ReportSection.model_validate(h["section"])
            for h in retrieval["hits"]
            if h["session_id"] == session.id
            and h["semantic_version"] == session.state.semantic_version
        ]
        sections_by_id = {s.message_id: s for s in workspace.sections}
        sections_by_id.update({s.message_id: s for s in retrieved_sections})
        if browser_director is not None and (requested is True or browser_advice):
            # Older single-skill browser queries don't populate AgentWorkspace.
            # Import reauthorized same-session attachments; never call GA4 or
            # label old results with the current session's period/filters.
            legacy = [
                m
                for m in await self.repository.list_messages(session.id)
                if m.visualization and m.id not in sections_by_id
            ][-8:]
            for restored in await self.orchestrator.restore_results(
                session=session, messages=legacy
            ):
                if (
                    restored.source_session_id
                    or not restored.findings
                    or not restored.evidence
                ):
                    continue
                sections_by_id[restored.message_id] = ReportSection(
                    title=restored.visualization.title[:200],
                    message_id=restored.message_id,
                    finding_ids=tuple(f.id for f in restored.findings),
                    evidence_ids=tuple(e.id for e in restored.evidence),
                    facts=tuple(f.text[:800] for f in restored.findings[:4]),
                    quality_notes=tuple(
                        dict.fromkeys(
                            note[:500]
                            for e in restored.evidence
                            for note in e.quality.notes
                        )
                    )[:4],
                    dimension_fields=restored.visualization.dimensions,
                    metric_fields=tuple(
                        dict.fromkeys(f.metric_field for f in restored.findings)
                    ),
                )
        ordered_sections = tuple(sections_by_id.values())
        workspace = await self.checkpoint(
            session,
            workspace,
            status="running",
            operation_id=operation_id,
            lease_until=utc_now() + timedelta(seconds=330),
            context_revision=site.revision,
            last_error=None,
            memory=working,
            sections=ordered_sections[-32:],
            completed_queries=0,
            report_requested=background or requested is True,
            execution_errors=(),
            stop_reason=None,
            review_targets=(),
            completed_perspectives=(),
            review_assumptions=(),
            workflow_id=workspace.workflow_id if resume else None,
            remaining_tasks=workspace.remaining_tasks if resume else (),
            execution_steps=(),
            required_dimension_fields=(),
            required_dimension_groups=(),
            required_metric_fields=(),
            required_period=None,
            required_comparison_period=None,
            comparison_required_exactly=False,
            required_filters=None,
            reused_section_ids=(),
            turns=(*workspace.turns[-19:], {"role": "user", "content": content[:4000]}),
        )
        if not background and re.search(
            r"覚えて|記憶して|今後の方針として保存", content
        ):
            await self.memory.change_agreement(
                user_id,
                session.data_source_set_id,
                AgreementChange(action="propose", text=content[:1000]),
                session_id=session.id,
                message_id=user_message_id,
            )
        message = AnalysisMessage(
            id="msg_" + record_id("user", session.id, operation_id)[:32],
            session_id=session.id,
            user_id=user_id,
            company_id=session.company_id,
            data_source_set_id=session.data_source_set_id,
            request_id=operation_id,
            role=MessageRole.USER,
            content=content,
        )
        try:
            if not await self.repository.create_message(message):
                raise AnalysisOrchestrationError("analysis_request_duplicate")
            await self.audit(
                user_id,
                session.data_source_set_id,
                "agent.turn",
                "started",
                request_id=operation_id,
                context_revision=site.revision,
            )
            query_signatures: set[str] = set()
            approved_staged = False
            decision: AgentDecision | None = None
            correction: str | None = None
            query_repair: QueryRepair | None = None
            repair_used = False
            errors: list[str] = []
            stop_reason: str | None = None
            agenda: ReviewAgenda | None = None
            attempted_perspectives: set[str] = set()
            summary: AgentSummary | None = None
            presentation_complete = False
            coverage_repair_attempted = False
            review_anchor: AnalysisState | None = None
            continuation: ContinuationContract | None = None
            unresolved_plan_error = False
            compiler = PlanCompiler()
            # Exploratory model decisions remain bounded. A validated dependent
            # plan has its own finite acquisition budget: rank + each target.
            # Deterministic child queries do not consume model planning rounds.
            planning_steps = 0
            query_limit = (
                (1 if workspace.staged_plan.phase == "rank" else 0)
                + (
                    workspace.staged_plan.request.limit
                    if workspace.staged_plan.phase == "rank"
                    else sum(i.status == "pending" for i in workspace.staged_plan.items)
                )
                if staged_active and workspace.staged_plan
                else 4
            )
            research_deadline = monotonic() + 240
            async with asyncio.timeout(300):
                for step in range(7 + 11):
                    if monotonic() >= research_deadline:
                        stop_reason = "application_time_budget"
                        break
                    if not staged_active:
                        if planning_steps >= 7:
                            stop_reason = "application_step_budget"
                            decision = None
                            break
                        planning_steps += 1
                    session = await self.session(user_id, session_id)
                    definitions = await self.repository.list_semantic_definitions(
                        company_id=session.company_id,
                        data_source_set_id=session.data_source_set_id,
                        version=session.state.semantic_version,
                    )
                    definitions = effective_definitions(definitions, session)
                    facts = await self.facts(session, workspace)
                    historical_context = retrieval["hits"]
                    if saved_data_only and preferred_evidence_ids:
                        # A similar report is not the report cited by the prior
                        # answer. Never substitute it when direct recovery fails.
                        cited = set(preferred_evidence_ids)
                        historical_context = [
                            hit
                            for hit in historical_context
                            if hit["facts"]
                            and all(
                                f["evidence_ids"]
                                and set(f["evidence_ids"]).issubset(cited)
                                for f in hit["facts"]
                            )
                        ]
                    if not background and re.search(
                        r"過去|前回|最初|以前|先ほど|さっき|前の", content
                    ):
                        facts = list(
                            {
                                f["id"]: f
                                for f in (
                                    *(
                                        f
                                        for hit in historical_context
                                        for f in hit["facts"]
                                    ),
                                    *facts,
                                )
                            }.values()
                        )[-32:]
                    scope = await self.authorization.authorize_session(user_id, session)
                    if step == 0:
                        explicit_periods = (
                            explicit_month_comparison(content)
                            if not background
                            else None
                        )
                        workspace = await self.checkpoint(
                            session,
                            workspace,
                            required_dimension_fields=explicit_dimension_fields(
                                content, definitions
                            ),
                            required_dimension_groups=(
                                joint_dimension_fields(content, definitions),
                            )
                            if joint_dimension_fields(content, definitions)
                            else (),
                            required_metric_fields=explicit_semantic_fields(
                                content, definitions, kind="metric"
                            ),
                            required_period=explicit_periods[0]
                            if explicit_periods
                            else None,
                            required_comparison_period=explicit_periods[1]
                            if explicit_periods
                            else None,
                            required_filters=explicit_filter_requirements(
                                content, session.state, definitions
                            ),
                            reused_section_ids=reusable_sections(
                                workspace, session.state, {f["id"] for f in facts}
                            )
                            if not background
                            and existing_result_discussion(content, definitions)
                            else (),
                        )
                        if workspace.reused_section_ids:
                            facts = await self.facts(session, workspace)
                        if not background and not saved_data_only and not staged_active:
                            continuation = condition_change_continuation(
                                content, session.state, definitions
                            ) or filter_continuation(
                                content, session.state, definitions
                            )
                            if continuation:
                                workspace = await self.checkpoint(
                                    session,
                                    workspace,
                                    **continuation.requirements(definitions),
                                )
                    if saved_data_only:
                        if workspace.staged_plan is None:
                            recovered_staged = await recover_staged(
                                self.repository,
                                self.store,
                                session,
                                historical_context,
                                preferred_evidence_ids=preferred_evidence_ids,
                                preferred_finding_ids=preferred_finding_ids,
                                required_dimension_fields=workspace.required_dimension_fields,
                                required_metric_fields=workspace.required_metric_fields,
                                required_period=workspace.required_period
                                or session.state.period,
                                required_comparison_period=workspace.required_comparison_period,
                                required_filters=workspace.required_filters,
                            )
                        # The app, not the LLM, enforces zero additional calls.
                        # Child reports have different filters: retain their
                        # explicit scope instead of forcing the parent's filter.
                        known = {f["id"] for f in facts}
                        workspace = await self.checkpoint(
                            session,
                            workspace,
                            reused_section_ids=tuple(
                                s.message_id
                                for s in workspace.sections
                                if s.finding_ids and set(s.finding_ids).issubset(known)
                            ),
                        )
                        decision = AgentDecision(
                            action="finish",
                            objective=workspace.objective,
                            explanation="追加取得を行わず、保存済みの根拠を評価します。",
                        )
                        break
                    if step == 0 and not background and not staged_active:
                        seed = explicit_half_year_review(
                            content, definitions, session.state
                        )
                        if seed:
                            agenda = ReviewAgenda.create(
                                seed, session.state, definitions
                            )
                            if agenda:
                                workspace = await self.checkpoint(
                                    session,
                                    workspace,
                                    objective=content[:500],
                                    review_targets=agenda.targets,
                                    review_assumptions=(
                                        "暦年の上期（1〜6月）／下期（7〜12月）として、前年同期と比較しています。"
                                        "会計年度の上期の場合は開始月を指定してください。",
                                    ),
                                )
                    zone = (
                        scope.source_set.ga4.timezone if scope.source_set.ga4 else "UTC"
                    )
                    direct_comparison = (
                        explicit_comparison_table(content, definitions)
                        if step == 0
                        and browser_director is not None
                        and not background
                        and not staged_active
                        else None
                    )
                    yield {
                        "event": "status",
                        "data": {
                            "stage": "取得に失敗した理由を確認し、再取得の条件を検討中"
                            if query_repair
                            else "追加調査を計画中"
                            if step
                            else "目的とこれまでの会話を確認中",
                            "step": step,
                        },
                    }
                    try:
                        async with asyncio.timeout(
                            max(0.01, research_deadline - monotonic())
                        ):
                            next_review = (
                                agenda.next_plan(attempted_perspectives)
                                if agenda
                                else None
                            )
                            staged_next = (
                                next_query(workspace.staged_plan)
                                if staged_active and workspace.staged_plan
                                else None
                            )
                            if staged_active and not staged_next:
                                decision = AgentDecision(
                                    action="finish",
                                    objective=workspace.staged_plan.objective
                                    if workspace.staged_plan
                                    else workspace.objective,
                                    explanation="各対象の取得結果を評価します。",
                                )
                                break
                            if agenda and next_review is None:
                                # All bounded perspectives attempted; reserve time for
                                # the independent, evidence-grounded answer stage.
                                decision = AgentDecision(
                                    action="finish",
                                    objective=workspace.objective,
                                    explanation="取得結果を評価します。",
                                )
                                break
                            decision = (
                                AgentDecision(
                                    action="ask",
                                    objective="分析の時間粒度を確認",
                                    explanation="時間粒度の指定が曖昧なため、取得前に確認します。",
                                    questions=(
                                        "日別・週別・月別・年別のどの粒度で確認しますか？"
                                        "複数必要な場合は、両方を指定してください。",
                                    ),
                                )
                                if grain_request.needs_clarification
                                and not saved_data_only
                                and not background
                                else AgentDecision(
                                    action="query",
                                    objective=workspace.objective,
                                    explanation="次の観点を確認します。",
                                    plan=next_review[1],
                                )
                                if next_review
                                else AgentDecision(
                                    action="query",
                                    objective=workspace.staged_plan.objective
                                    if workspace.staged_plan
                                    else workspace.objective,
                                    explanation="保存済みの段階計画を進めます。",
                                    plan=staged_next[0],
                                )
                                if staged_next
                                else AgentDecision(
                                    action="query",
                                    objective="指定された期間の内訳比較",
                                    explanation="明示された指標・集計軸で比較します。",
                                    plan=direct_comparison,
                                )
                                if direct_comparison
                                else await director.decide(
                                    {
                                        "user_question": content,
                                        "today": utc_now()
                                        .astimezone(ZoneInfo(zone))
                                        .date()
                                        .isoformat(),
                                        "timezone": zone,
                                        "site_context": site_understanding(site),
                                        "prior_goal_context": prior_goal_context(
                                            workspace, site, facts
                                        ),
                                        "working_memory": workspace.memory.model_dump(
                                            mode="json"
                                        ),
                                        "confirmed_agreements": [
                                            a.model_dump(mode="json")
                                            for a in agreements
                                            if a.status == "confirmed"
                                        ],
                                        "historical_analyses": historical_context,
                                        "memory_search_truncated": retrieval[
                                            "truncated"
                                        ],
                                        "workspace": planning_workspace(
                                            workspace, {f["id"] for f in facts}
                                        ),
                                        "analysis_state": query_context(session.state),
                                        "continuation_contract": continuation.context()
                                        if continuation
                                        else None,
                                        "facts": facts,
                                        "analysis_capabilities": capability_context(
                                            definitions
                                        ),
                                        "missing_requested_fields": self.missing_fields(
                                            workspace
                                        ),
                                        "required_grouped_request": (
                                            grouped.model_dump(mode="json")
                                            if (
                                                grouped := explicit_grouped_request(
                                                    content
                                                )
                                            )
                                            and not background
                                            else None
                                        ),
                                        "data_acquisition_allowed": not saved_data_only,
                                        "catalog": [
                                            {
                                                "term": d.canonical_name,
                                                "kind": d.kind,
                                                "aliases": d.aliases,
                                                "aggregation": d.aggregation,
                                                "ga4_mapping": d.ga4_mapping,
                                                "unit": d.unit,
                                            }
                                            for d in definitions
                                            if d.enabled and d.available
                                        ],
                                        "skills": [
                                            s.model_dump(mode="json")
                                            for s in DEFAULT_SKILL_REGISTRY.list()
                                            if s.id.value != "ga4.metadata_discovery.v1"
                                        ],
                                        "query_budget": max(
                                            0, query_limit - workspace.completed_queries
                                        ),
                                        "background": background,
                                        "scheduled_period": period.model_dump(
                                            mode="json"
                                        )
                                        if period
                                        else None,
                                        "validation_error": correction,
                                        "query_repair": query_repair.context()
                                        if query_repair
                                        else None,
                                    }
                                )
                            )
                    except PlannerPhaseValidationError as error:
                        # One bounded local schema repair already ran. Do not
                        # start the controller over repeatedly with the same data.
                        errors.append(error.error_code)
                        stop_reason = "planning_failed"
                        unresolved_plan_error = True
                        logging.getLogger(__name__).warning(
                            "Agent planning phase validation failed",
                            extra={
                                "error_code": error.error_code,
                                "request_id": operation_id,
                                "validation_fields": error.feedback,
                            },
                        )
                        decision = None
                        break
                    except PlannerPhaseTimeout as error:
                        errors.append(f"agent_{error.phase}_timeout")
                        stop_reason = "planning_phase_timeout"
                        decision = None
                        break
                    except ValidationError as error:
                        correction = validation_feedback(error)
                        errors.append("invalid_schema_or_generated_numbers")
                        unresolved_plan_error = True
                        logging.getLogger(__name__).warning(
                            "Agent plan validation failed",
                            extra={
                                "error_code": "invalid_schema_or_generated_numbers",
                                "validation_fields": [
                                    {"path": e["loc"], "type": e["type"]}
                                    for e in error.errors(
                                        include_input=False, include_context=False
                                    )[:4]
                                ],
                            },
                        )
                        decision = None
                        continue
                    except TimeoutError:
                        errors.append("application_time_budget")
                        stop_reason = "application_time_budget"
                        decision = None
                        break
                    except Exception as error:
                        # The planning provider can fail before any GA4 call.
                        # Preserve the evidence-answer path instead of treating a
                        # provider/SDK error as a user pressing Stop.
                        logging.getLogger(__name__).warning(
                            "Agent planning provider failed",
                            extra={
                                "error_code": "agent_planner_unavailable",
                                "error_type": type(error).__name__,
                                "request_id": operation_id,
                                "provider_status": getattr(error, "code", None)
                                if isinstance(getattr(error, "code", None), int)
                                else None,
                            },
                        )
                        errors.append("agent_planner_unavailable")
                        if getattr(error, "code", None) == 429:
                            errors.append("agent_model_rate_limited")
                        stop_reason = "planning_failed"
                        decision = None
                        break
                    try:
                        self.validate_decision(
                            decision,
                            site,
                            facts,
                            {
                                d.canonical_name
                                for d in definitions
                                if d.kind == "metric" and d.available and d.enabled
                            },
                        )
                        missing = self.missing_fields(workspace)
                        if (
                            decision.action == "finish"
                            and workspace.completed_queries < 4
                            and missing
                            and not coverage_repair_attempted
                        ):
                            coverage_repair_attempted = True
                            raise ValueError(
                                "requested_analysis_not_observed: "
                                + ", ".join(missing)
                                + "; obtain these fields with the requested periods, "
                                "or explain why unavailable; "
                                "do not repeat failed queries"
                            )
                        observations = {
                            (field, str(row[field]))
                            for section in workspace.sections
                            for row in section.observations
                            for field in section.dimension_fields
                            if field in row and row[field] is not None
                        }
                        if any(
                            (f.field, f.value) not in observations
                            for f in decision.observed_filters
                        ):
                            raise ValueError(
                                "filter_value_not_in_authorized_observations"
                            )
                    except ValueError as error:
                        correction = str(error)
                        errors.append(correction)
                        decision = None
                        continue
                    correction = None
                    unresolved_plan_error = False
                    # Model-selected memory is accepted only as a verbatim,
                    # attributed user statement, never an inferred measured fact.
                    entries = list(workspace.memory.entries)
                    for quote in decision.memory_quotes:
                        if quote.text not in content:
                            continue
                        entries = [
                            e
                            for e in entries
                            if (e.kind, e.text) != (quote.kind, quote.text)
                        ]
                        entries.append(
                            MemoryEntry(
                                text=quote.text,
                                kind=quote.kind,
                                message_id=user_message_id,
                            )
                        )
                    workspace = await self.checkpoint(
                        session,
                        workspace,
                        objective=decision.objective,
                        memory=workspace.memory.model_copy(
                            update={
                                "entries": tuple(entries[-40:]),
                                "trimmed": workspace.memory.trimmed
                                or len(entries) > 40,
                            }
                        ),
                        questions=decision.questions,
                        open_questions=decision.open_questions,
                        workflow_id=decision.workflow_id or workspace.workflow_id,
                        remaining_tasks=decision.remaining_tasks,
                        review_assumptions=tuple(
                            dict.fromkeys(
                                (*workspace.review_assumptions, *decision.assumptions)
                            )
                        ),
                    )
                    if decision.action != "query":
                        break
                    if (
                        workspace.completed_queries >= query_limit
                        or decision.plan is None
                    ):
                        stop_reason = "application_query_budget"
                        decision = None
                        break
                    plan = decision.plan
                    if grain_request.single and not staged_active and not background:
                        # Use the same affirmative grain for acquisition and
                        # completion. The compiler replaces stale date axes.
                        plan = plan.model_copy(
                            update={"time_grain": grain_request.single}
                        )
                        categorical = [
                            field
                            for field in workspace.required_dimension_fields
                            if field not in TIME_TABLE_FIELDS
                        ]
                        if (
                            browser_director is not None
                            and len(categorical) == 1
                            and is_channel_table_field(categorical[0])
                            and not re.search(r"ではなく|除外|の代わり", content)
                        ):
                            channel_terms = [
                                d.canonical_name
                                for d in definitions
                                if d.kind == "dimension"
                                and d.ga4_mapping
                                and d.ga4_mapping.get("dimension") == categorical[0]
                            ]
                            if len(channel_terms) == 1:
                                table = bool(
                                    re.search(r"表(?:形式)?(?:で|に)|一覧", content)
                                )
                                ranking = VertexIntentPlanner._ranking_from_question(
                                    content
                                )
                                plan = plan.model_copy(
                                    update={
                                        "dimension_terms": tuple(channel_terms),
                                        "skill_id": AnalysisSkillId.CROSS_BREAKDOWN,
                                        "ranking": ranking,
                                        "detail": table or plan.detail,
                                        "preferred_chart_type": "table"
                                        if table
                                        else plan.preferred_chart_type,
                                    }
                                )
                    grouped = decision.grouped_request or (
                        explicit_grouped_request(content) if step == 0 else None
                    )
                    if grouped and not staged_active and not background:
                        try:
                            initial_filters = tuple(
                                AnalysisFilter(
                                    semantic_name=observed_name(f.field, f.value),
                                    kind="filter",
                                    field_name=f.field,
                                    value=f.value,
                                )
                                for f in decision.observed_filters
                            )
                            staged_state = session.state.model_copy(
                                update={
                                    "filters": (
                                        *(
                                            ()
                                            if plan.clear_filters
                                            else session.state.filters
                                        ),
                                        *initial_filters,
                                    )
                                }
                            )
                            staged_base = plan.model_copy(
                                update={
                                    "filter_terms": (
                                        *plan.filter_terms,
                                        *(f.semantic_name for f in initial_filters),
                                    )
                                }
                            )
                            staged = create_staged(
                                grouped, staged_base, staged_state, definitions, content
                            )
                            staged = staged.model_copy(
                                update={
                                    "source_fingerprint": record_id(
                                        str(scope.source_set.ga4)
                                    )
                                }
                            )
                        except ValueError as error:
                            errors.append(str(error))
                            stop_reason = "incomplete_requested_analysis"
                            decision = None
                            break
                        workspace = await self.checkpoint(
                            session, workspace, staged_plan=staged
                        )
                        query_limit = (
                            workspace.completed_queries + 1 + staged.request.limit
                        )
                        staged_active = True
                        agenda = None
                    if staged_active and workspace.staged_plan:
                        selected = next_query(workspace.staged_plan)
                        if selected is None:
                            break
                        plan = selected[0]
                    if (
                        not staged_active
                        and workspace.required_dimension_groups
                        and plan.skill_id
                        not in {
                            AnalysisSkillId.REALTIME,
                            AnalysisSkillId.FUNNEL,
                            AnalysisSkillId.ANOMALY_SCAN,
                            AnalysisSkillId.METADATA_DISCOVERY,
                        }
                        and not plan.visualization_only
                        and self.missing_fields(workspace)
                    ):
                        # Explicit co-observed axes are part of the user's query
                        # contract. Never substitute an independent KPI request.
                        fields = workspace.required_dimension_groups[0]
                        terms = {
                            str(d.ga4_mapping["dimension"]): d.canonical_name
                            for d in definitions
                            if d.ga4_mapping
                            and "dimension" in d.ga4_mapping
                            and d.kind in {"dimension", "date"}
                        }
                        if all(field in terms for field in fields):
                            grain = VertexIntentPlanner._time_grain_from_question(
                                content
                            )
                            plan = plan.model_copy(
                                update={
                                    "dimension_terms": tuple(
                                        terms[field] for field in fields
                                    ),
                                    "skill_id": AnalysisSkillId.CROSS_BREAKDOWN,
                                    "time_grain": grain,
                                }
                            )
                    if (
                        workspace.required_period
                        and workspace.required_comparison_period
                    ):
                        plan = plan.model_copy(
                            update={
                                "period": workspace.required_period,
                                "comparison_period": (
                                    workspace.required_comparison_period
                                ),
                                "comparison": ComparisonIntent.NONE,
                                "clear_comparison": False,
                            }
                        )
                        if (
                            browser_director is not None
                            and not staged_active
                            and not grain_request.mentioned
                            and not re.search(
                                r"推移|時系列|ではなく|除外|の代わり|日ごと|月ごと",
                                content,
                            )
                            and 1 <= len(workspace.required_dimension_fields) <= 4
                            and 1 <= len(workspace.required_metric_fields) <= 4
                        ):
                            dim_terms = {
                                d.ga4_mapping["dimension"]: d.canonical_name
                                for d in definitions
                                if d.ga4_mapping
                                and d.kind == "dimension"
                                and "dimension" in d.ga4_mapping
                            }
                            metric_terms = {
                                d.ga4_mapping["metric"]: d.canonical_name
                                for d in definitions
                                if d.ga4_mapping
                                and d.kind == "metric"
                                and "metric" in d.ga4_mapping
                            }
                            requested_axes = workspace.required_dimension_fields
                            requested_metrics = workspace.required_metric_fields
                            if all(d in dim_terms for d in requested_axes) and all(
                                m in metric_terms for m in requested_metrics
                            ):
                                # Two named dates are period bounds, not a date
                                # grouping. Do not inherit a stray old day axis.
                                table = bool(
                                    re.search(r"表(?:形式)?(?:で|に)|一覧", content)
                                )
                                ranking = VertexIntentPlanner._ranking_from_question(
                                    content
                                )
                                plan = plan.model_copy(
                                    update={
                                        "intent": "指定期間の内訳を同じ項目で比較",
                                        "dimension_terms": tuple(
                                            dim_terms[d] for d in requested_axes
                                        ),
                                        "metric_terms": tuple(
                                            metric_terms[m] for m in requested_metrics
                                        ),
                                        "time_grain": None,
                                        "ranking": ranking,
                                        "skill_id": AnalysisSkillId.COMPARE
                                        if len(requested_axes) <= 2
                                        else AnalysisSkillId.CROSS_BREAKDOWN,
                                        "detail": table or plan.detail,
                                        "preferred_chart_type": "table"
                                        if table
                                        else plan.preferred_chart_type,
                                    }
                                )
                    if (
                        not staged_active
                        and agenda is None
                        and is_multifaceted_review(content)
                        and workspace.completed_queries == 0
                        and not decision.observed_filters
                        and not plan.dimension_terms
                        and decision.workflow_id != "analysis.acquisition_comparison.v1"
                    ):
                        agenda = ReviewAgenda.create(plan, session.state, definitions)
                        if agenda:
                            workspace = await self.checkpoint(
                                session, workspace, review_targets=agenda.targets
                            )
                    perspective = None
                    if agenda:
                        next_review = agenda.next_plan(attempted_perspectives)
                        if next_review:
                            perspective, plan = next_review
                            attempted_perspectives.add(perspective)
                    observed_filters = tuple(
                        AnalysisFilter(
                            semantic_name=observed_name(f.field, f.value),
                            kind="filter",
                            field_name=f.field,
                            value=f.value,
                        )
                        for f in decision.observed_filters
                    )
                    if staged_active and workspace.staged_plan:
                        selected = next_query(workspace.staged_plan)
                        if selected:
                            observed_filters = selected[1]
                    if observed_filters:
                        plan = plan.model_copy(
                            update={
                                "filter_terms": (
                                    *plan.filter_terms,
                                    *(f.semantic_name for f in observed_filters),
                                )
                            }
                        )
                    if period:
                        plan = plan.model_copy(update={"period": period})
                    try:
                        observed_definitions = [
                            observed_definition(session, f)
                            for f in (*session.state.filters, *observed_filters)
                            if f.semantic_name.startswith("observed_")
                        ]
                        compiled = compiler.compile(
                            plan,
                            state=session.state,
                            scope=scope,
                            definitions=[*definitions, *observed_definitions],
                        )
                        if continuation:
                            continuation.validate(compiled, state=session.state)
                    except ValueError as error:
                        correction = str(error)
                        errors.append(correction)
                        if direct_comparison or query_repair:
                            stop_reason = "planning_failed"
                            presentation_complete = True
                            decision = None
                            break
                        continue
                    if query_repair and not query_repair.accepts(
                        compiled, session.state, workspace
                    ):
                        errors.append("ga4_query_repair_rejected")
                        stop_reason = "tool_failed"
                        decision = None
                        break
                    if agenda and perspective == agenda.targets[0]:
                        # Capture validated primary CONDITIONS even if that
                        # request later fails. Auxiliary results must not choose
                        # the conversational axis. No result/evidence is invented.
                        review_anchor = (
                            AnalysisStateReducer()
                            .apply(session.state, compiled.delta)
                            .state
                        )
                    if staged_active and workspace.staged_plan:
                        review_anchor = workspace.staged_plan.anchor
                    signature = query_signature(plan, compiled.arguments)
                    if signature in query_signatures:
                        correction = "duplicate_query_choose_another_analysis_or_finish"
                        errors.append(correction)
                        continue
                    if browser_director is not None and (
                        not staged_active or not approved_staged
                    ):
                        if browser_approval is None:
                            raise AnalysisOrchestrationError(
                                "browser_plan_confirmation_required"
                            )
                        approved_state = (
                            AnalysisStateReducer()
                            .apply(session.state, compiled.delta)
                            .state
                        )
                        comparison = approved_state.comparison_period
                        confirmed = await browser_approval(
                            {
                                "source": scope.source_set.name,
                                "period": approved_state.period.model_dump(mode="json")
                                if approved_state.period
                                else None,
                                "comparison_period": comparison.model_dump(mode="json")
                                if comparison
                                else None,
                                "metrics": [m.label for m in compiled.metrics],
                                "dimensions": [d.label for d in compiled.dimensions],
                                "filters": [
                                    {"field": f.field_name, "value": f.value}
                                    for f in approved_state.filters
                                ],
                                "limit": plan.ranking.limit if plan.ranking else None,
                                "presentation": compiled.preferred_chart_type,
                                "retry_reason": query_repair.code
                                if query_repair
                                else None,
                                "staged": {
                                    "primary": workspace.staged_plan.primary_field,
                                    "secondary": workspace.staged_plan.secondary_field,
                                    "limit": workspace.staged_plan.request.limit,
                                }
                                if staged_active and workspace.staged_plan
                                else None,
                            }
                        )
                        if not confirmed:
                            raise AnalysisOrchestrationError("browser_plan_declined")
                        latest = await self.session(user_id, session_id)
                        latest_scope = await self.authorization.authorize_session(
                            user_id, latest
                        )
                        if (
                            latest.revision != session.revision
                            or latest_scope.source_set != scope.source_set
                        ):
                            raise AnalysisOrchestrationError(
                                "analysis_session_conflict"
                            )
                        approved_staged = staged_active
                    query_signatures.add(signature)
                    workspace = await self.checkpoint(
                        session,
                        workspace,
                        completed_queries=workspace.completed_queries + 1,
                        execution_steps=(
                            *workspace.execution_steps,
                            AgentExecutionStep(
                                id=f"{operation_id}:query:{step}",
                                plan=plan,
                                status="running",
                            ),
                        ),
                    )
                    yield {
                        "event": "status",
                        "data": {
                            "stage": (
                                f"調査 {workspace.completed_queries}: {plan.intent}"
                            ),
                            "step": step,
                            "completed_queries": workspace.completed_queries - 1,
                            "planned_query_limit": query_limit,
                        },
                    }
                    try:
                        async with asyncio.timeout(
                            max(0.01, research_deadline - monotonic())
                        ):
                            result = await self.orchestrator.analyze(
                                user_id=user_id,
                                session_id=session_id,
                                content="調査: " + plan.intent,
                                request_id=operation_id,
                                idempotency_key=f"{operation_id}:query:{step}",
                                trusted_plan=plan,
                                observed_filters=observed_filters,
                                browser_agent=browser_director is not None,
                            )
                    except TimeoutError:
                        workspace = await self.checkpoint(
                            session,
                            workspace,
                            execution_steps=(
                                *workspace.execution_steps[:-1],
                                workspace.execution_steps[-1].model_copy(
                                    update={
                                        "status": "failed",
                                        "error_code": "application_time_budget",
                                    }
                                ),
                            ),
                        )
                        stop_reason = "application_time_budget"
                        errors.append("application_time_budget")
                        decision = None
                        break
                    except AnalysisOrchestrationError as error:
                        if error.code in {
                            "ga4_property_access_denied",
                            "ga4_reauthorization_required",
                            "access_denied",
                        }:
                            raise
                        correction = error.code
                        errors.append(correction)
                        workspace = await self.checkpoint(
                            session,
                            workspace,
                            execution_steps=(
                                *workspace.execution_steps[:-1],
                                workspace.execution_steps[-1].model_copy(
                                    update={
                                        "status": "failed",
                                        "error_code": error.code,
                                    }
                                ),
                            ),
                        )
                        if (
                            browser_director is not None
                            and not staged_active
                            and agenda is None
                            and not repair_used
                            and compiled.tool_name == "run_report"
                            and error.code in RECOVERABLE_GA4_ERRORS
                            and workspace.completed_queries < query_limit
                        ):
                            repair_used = True
                            query_repair = QueryRepair(
                                code=error.code,
                                previous=compiled,
                                state=AnalysisStateReducer()
                                .apply(session.state, compiled.delta)
                                .state,
                                fixed_row_count=(
                                    VertexIntentPlanner._ranking_from_question(content)
                                    is not None
                                    or bool(
                                        re.search(
                                            r"全(?:件|行)|すべて|全て|網羅|漏れなく",
                                            content,
                                        )
                                    )
                                ),
                            )
                            continue
                        if staged_active or browser_director is not None:
                            stop_reason = "tool_failed"
                            presentation_complete = direct_comparison is not None
                            decision = None
                            break
                        continue
                    except AccessDenied:
                        raise
                    except Exception as error:
                        logging.getLogger(__name__).warning(
                            "Agent analytics execution failed",
                            extra={
                                "error_code": "agent_tool_failed",
                                "error_type": type(error).__name__,
                                "request_id": operation_id,
                            },
                        )
                        workspace = await self.checkpoint(
                            session,
                            workspace,
                            execution_steps=(
                                *workspace.execution_steps[:-1],
                                workspace.execution_steps[-1].model_copy(
                                    update={
                                        "status": "failed",
                                        "error_code": "agent_tool_failed",
                                    }
                                ),
                            ),
                        )
                        errors.append("agent_tool_failed")
                        stop_reason = "tool_failed"
                        decision = None
                        break
                    workspace = await self.checkpoint(
                        result.session,
                        workspace,
                        execution_steps=(
                            *workspace.execution_steps[:-1],
                            workspace.execution_steps[-1].model_copy(
                                update={
                                    "status": "succeeded",
                                    "evidence_ids": tuple(
                                        e.id for e in result.evidence
                                    ),
                                }
                            ),
                        ),
                        sections=(
                            *(
                                s
                                if index >= len(workspace.sections) - 3
                                else s.model_copy(update={"observations": ()})
                                for index, s in enumerate(workspace.sections)
                                if index >= len(workspace.sections) - 23
                            ),
                            ReportSection(
                                title=plan.intent,
                                period=result.session.state.period,
                                comparison_period=result.session.state.comparison_period,
                                filters=result.session.state.filters,
                                operation_id=operation_id,
                                perspective=perspective,
                                message_id=result.assistant_message.id,
                                finding_ids=tuple(f.id for f in result.findings),
                                evidence_ids=tuple(e.id for e in result.evidence),
                                facts=tuple(f.text[:800] for f in result.findings[:4]),
                                quality_notes=tuple(
                                    dict.fromkeys(
                                        note[:500]
                                        for e in result.evidence
                                        for note in e.quality.notes
                                    )
                                )[:4],
                                dimension_fields=tuple(
                                    d.field for d in compiled.dimensions
                                ),
                                metric_fields=tuple(
                                    m.field
                                    for m in compiled.metrics
                                    if any(
                                        row.get(m.field) is not None
                                        for row in result.rows
                                    )
                                ),
                                observed_periods=tuple(
                                    sorted(
                                        {
                                            str(row.get("dateRange", "current"))
                                            for row in result.rows
                                        }
                                    )
                                ),
                                observations=tuple(
                                    {
                                        k: v
                                        for k, v in row.items()
                                        if k
                                        in {
                                            *(d.field for d in compiled.dimensions),
                                            *(m.field for m in compiled.metrics),
                                            "dateRange",
                                        }
                                        if not isinstance(v, str) or len(v) <= 160
                                    }
                                    for row in result.rows[:12]
                                ),
                            ),
                        ),
                        completed_perspectives=(
                            *workspace.completed_perspectives,
                            perspective,
                        )
                        if perspective
                        else workspace.completed_perspectives,
                    )
                    await self.memory.archive(
                        result.session, content, workspace.sections[-1]
                    )
                    if staged_active and workspace.staged_plan:
                        staged = record_result(
                            workspace.staged_plan,
                            result.rows,
                            result.session.state,
                            tuple(e.id for e in result.evidence),
                        )
                        workspace = await self.checkpoint(
                            result.session, workspace, staged_plan=staged
                        )
                        review_anchor = staged.anchor
                    yield {"event": "analysis", "data": result.model_dump(mode="json")}
                    query_repair = None
                    if direct_comparison or (
                        browser_director is not None
                        and not staged_active
                        and agenda is None
                        and workspace.required_dimension_fields
                        and workspace.required_metric_fields
                        and not self.missing_fields(workspace)
                        and result.visualization is not None
                        and re.search(
                            r"(?:表(?:形式)?|一覧|グラフ|チャート)(?:に|で)"
                            r"(?:してください|見せて(?:ください)?|表示してください|"
                            r"出して(?:ください)?)[。！!\s]*$",
                            content,
                        )
                        and not re.search(
                            r"改善|提案|解釈|評価|考察|原因|仮説|なぜ|深掘|レポート|"
                            r"多角|それぞれ|各ページ|傾向を分析",
                            content,
                        )
                    ):
                        # A bounded display request is complete when the
                        # shared coverage check succeeds. Do not make the
                        # browser ask the model to discover the same query.
                        decision = AgentDecision(
                            action="finish",
                            objective=workspace.objective,
                            explanation="指定された項目の表示を準備しました。",
                        )
                        presentation_complete = True
                        break
                session = await self.session(user_id, session_id)
                facts = await self.facts(session, workspace)
                if (
                    not background
                    and not staged_active
                    and re.search(r"過去|前回|最初|以前|先ほど|さっき|前の", content)
                ):
                    facts = list(
                        {
                            f["id"]: f
                            for f in (
                                *(
                                    f
                                    for hit in historical_context
                                    for f in hit["facts"]
                                ),
                                *facts,
                            )
                        }.values()
                    )[-32:]
                missing_dimensions = self.missing_fields(workspace)
                staged_answer = bool(
                    (workspace.staged_plan and (staged_active or saved_data_only))
                    or recovered_staged
                )
                answer_plan = (
                    recovered_staged.plan if recovered_staged else workspace.staged_plan
                )
                answer_source = (
                    recovered_staged.source_session if recovered_staged else session
                )
                if staged_answer and answer_plan:
                    await self.validate_staged(answer_source, answer_plan)
                    # The contract is rank+all children, not a joint query.
                    missing_dimensions = (
                        tuple(
                            i.value for i in answer_plan.items if i.status == "pending"
                        )
                        + tuple(
                            sorted(
                                set(workspace.required_metric_fields)
                                - {answer_plan.metric_field}
                            )
                        )
                        + tuple(
                            sorted(
                                set(workspace.required_dimension_fields)
                                - {
                                    answer_plan.primary_field,
                                    answer_plan.secondary_field,
                                }
                            )
                        )
                    )
                    if answer_plan.phase == "rank":
                        missing_dimensions = ("上位対象の確定",)
                    facts = await self.facts(
                        session,
                        workspace.model_copy(
                            update={
                                "reused_section_ids": tuple(
                                    s.message_id
                                    for s in workspace.sections
                                    if set(s.evidence_ids)
                                    & (
                                        set(answer_plan.rank_evidence_ids)
                                        | {
                                            e
                                            for item in answer_plan.items
                                            for e in item.evidence_ids
                                        }
                                    )
                                )
                            }
                        ),
                    )
                    if recovered_staged:
                        historical_context = list(recovered_staged.hits)
                        facts = [f for h in recovered_staged.hits for f in h["facts"]]
                if browser_advice and browser_director is not None:
                    yield {
                        "event": "status",
                        "data": {"stage": "根拠とサイト設定から改善案を検討中"},
                    }
                    try:
                        candidate = await browser_director.advise(
                            {
                                "user_question": content,
                                "data_acquisition_allowed": False,
                                "facts": facts,
                                "site_context": site_understanding(site),
                                "prior_goal_context": prior_goal_context(
                                    workspace, site, facts
                                ),
                                "working_memory": workspace.memory.model_dump(
                                    mode="json"
                                ),
                                "confirmed_agreements": [
                                    a.model_dump(mode="json")
                                    for a in agreements
                                    if a.status == "confirmed"
                                ],
                                "analysis_state": query_context(session.state),
                                "analysis_capabilities": capability_context(
                                    definitions
                                ),
                                "catalog": [
                                    {"term": d.canonical_name, "kind": d.kind}
                                    for d in definitions
                                    if d.enabled and d.available
                                ],
                            }
                        )
                        self.validate_decision(
                            candidate,
                            site,
                            facts,
                            {
                                d.canonical_name
                                for d in definitions
                                if d.kind == "metric" and d.enabled and d.available
                            },
                        )
                        decision = candidate
                    except (
                        PlannerPhaseValidationError,
                        PlannerPhaseTimeout,
                    ) as error:
                        errors.append(
                            error.error_code
                            if isinstance(error, PlannerPhaseValidationError)
                            else "agent_advice_timeout"
                        )
                        logging.getLogger(__name__).warning(
                            "Browser advice was not accepted",
                            extra={
                                "request_id": operation_id,
                                "error_code": errors[-1],
                                "validation_fields": error.feedback
                                if isinstance(error, PlannerPhaseValidationError)
                                else None,
                            },
                        )
                        stop_reason = "planning_failed"
                    except (ValueError, AnalysisOrchestrationError):
                        errors.append("agent_advice_unavailable")
                        stop_reason = "planning_failed"
                incomplete = (
                    decision is None
                    or decision.action == "query"
                    or stop_reason is not None
                    or bool(
                        missing_dimensions and decision and decision.action != "ask"
                    )
                    or bool(
                        agenda
                        and set(agenda.targets) - set(workspace.completed_perspectives)
                    )
                )
                if incomplete:
                    stop_reason = stop_reason or (
                        "incomplete_requested_analysis"
                        if missing_dimensions
                        else "incomplete_perspectives"
                        if agenda and agenda.next_plan(attempted_perspectives) is None
                        else "application_query_budget"
                        if workspace.completed_queries >= query_limit
                        else "application_step_budget"
                    )
                saved_report_only = (
                    browser_director is not None
                    and saved_data_only
                    and workspace.report_requested
                    and not re.search(
                        r"評価|考察|解釈|仮説|改善|提案|課題|要因|理由", content
                    )
                )
                if (
                    not presentation_complete
                    and not saved_report_only
                    # Structured proposals and their evidence are rendered
                    # directly; after rejection keep the same facts and error.
                    and not browser_advice
                    and (incomplete or (decision and decision.action == "finish"))
                ):
                    yield {
                        "event": "status",
                        "data": {"stage": "取得済みデータと次の分析候補を整理中"},
                    }
                    try:
                        async with asyncio.timeout(
                            85 if browser_director is not None else 55
                        ):
                            summary = await director.summarize(
                                {
                                    "user_question": content,
                                    "facts": facts,
                                    "analysis_state": query_context(
                                        review_anchor or session.state
                                    ),
                                    "site_context": site_understanding(site),
                                    "prior_goal_context": prior_goal_context(
                                        workspace, site, facts
                                    ),
                                    "proposals": [
                                        p.model_dump(mode="json")
                                        for p in (
                                            decision.proposals if decision else ()
                                        )
                                    ],
                                    "analysis_capabilities": capability_context(
                                        definitions
                                    ),
                                    "working_memory": workspace.memory.model_dump(
                                        mode="json"
                                    ),
                                    "confirmed_agreements": [
                                        a.model_dump(mode="json")
                                        for a in agreements
                                        if a.status == "confirmed"
                                    ],
                                    "historical_analyses": historical_context,
                                    "staged_analysis": answer_plan.model_dump(
                                        mode="json"
                                    )
                                    if staged_answer and answer_plan
                                    else None,
                                    "review_assumptions": workspace.review_assumptions,
                                    "coverage": {
                                        "requested": workspace.review_targets,
                                        "completed": workspace.completed_perspectives,
                                        "missing": [
                                            t
                                            for t in workspace.review_targets
                                            if t not in workspace.completed_perspectives
                                        ],
                                    },
                                    "stop_reason": stop_reason,
                                    "execution_errors": [
                                        e
                                        for e in errors[-7:]
                                        if e != "invalid_schema_or_generated_numbers"
                                        or unresolved_plan_error
                                    ],
                                    "missing_requested_fields": sorted(
                                        missing_dimensions
                                    ),
                                    "data_acquisition_allowed": not saved_data_only,
                                    "observed_joint_axes": [
                                        list(s.dimension_fields)
                                        for s in workspace.sections
                                        if s.operation_id == operation_id
                                        or s.message_id in workspace.reused_section_ids
                                    ],
                                    "catalog": [
                                        {"term": d.canonical_name, "kind": d.kind}
                                        for d in definitions
                                        if d.enabled and d.available
                                    ],
                                }
                            )
                        known = {f["id"] for f in facts}
                        if any(
                            not set(a.fact_ids).issubset(known)
                            for a in summary.assessments
                        ):
                            raise ValueError("summary_unknown_fact_reference")
                    except Exception as error:
                        logging.getLogger(__name__).warning(
                            "Agent summary failed",
                            extra={
                                "error_code": "agent_summary_unavailable",
                                "request_id": operation_id,
                                "provider_status": getattr(error, "code", None)
                                if isinstance(getattr(error, "code", None), int)
                                else None,
                                "error_type": type(error).__name__,
                                "validation_fields": validation_feedback(error)
                                if isinstance(error, ValidationError)
                                else None,
                            },
                        )
                        errors.append("agent_summary_unavailable")
                        if isinstance(error, GroundingRejected):
                            errors.append("agent_summary_grounding_failed")
                        if getattr(error, "code", None) == 429:
                            errors.append("agent_model_rate_limited")
                        summary = None
                final_status = (
                    "partial"
                    if incomplete or "agent_summary_unavailable" in errors
                    else "waiting"
                    if decision and decision.action == "ask"
                    else "complete"
                )
                report_generated = workspace.report_requested and bool(facts)
                proposals_for_body = decision.proposals if decision else ()
                if (
                    report_generated
                    and saved_report_only
                    and workspace.proposals_site_revision == site.revision
                ):
                    authorized_evidence = {
                        eid for fact in facts for eid in fact["evidence_ids"]
                    }
                    proposals_for_body = tuple(
                        proposal
                        for proposal in workspace.proposals
                        if set(proposal.evidence_ids).issubset(authorized_evidence)
                    )
                body = render_summary(
                    summary,
                    facts,
                    workspace.review_targets,
                    workspace.completed_perspectives,
                    include_breakdowns=not staged_answer,
                    proposals=proposals_for_body,
                )
                if staged_answer and answer_plan:
                    body += "\n\n" + render_progress(answer_plan)
                if missing_dimensions and not staged_answer:
                    labels = {**DIMENSION_LABELS, **METRIC_LABELS}
                    body += (
                        "\n\n未確認の指定条件: "
                        + "、".join(
                            " × ".join(
                                labels.get(part, part) for part in f.split(" × ")
                            )
                            for f in missing_dimensions
                        )
                        + "。全体KPIだけでは指定された内訳・比較を評価できません。"
                    )
                if workspace.review_assumptions:
                    body = (
                        "## 分析の前提\n\n"
                        + "\n".join(
                            "- " + text(v) for v in workspace.review_assumptions
                        )
                        + "\n\n"
                        + body
                    )
                if decision and decision.action == "ask" and not incomplete:
                    body = (
                        prose(decision.explanation)
                        + "\n\n"
                        + "\n".join("- " + text(v) for v in decision.questions)
                    )
                if incomplete:
                    reason_label = {
                        "application_query_budget": (
                            f"分析計画の取得回数（最大{query_limit}回）"
                        ),
                        "application_step_budget": "計画・再検討の回数（最大7回）",
                        "application_time_budget": "調査時間（最大240秒）",
                    }.get(stop_reason or "", "実行予算")
                    body += (
                        "\n計画生成に失敗したため、追加取得は行わず確認済みの範囲を整理しました。"
                        if stop_reason == "planning_failed"
                        else (
                            "\nAIの計画段階が応答期限に達したため、確認済みの範囲を整理しました。"
                            "調査全体の時間上限やGA4 APIの利用上限とは別の制限です。"
                        )
                        if stop_reason == "planning_phase_timeout"
                        else (
                            "\n分析ツールで処理が失敗しました。確認済みの範囲を整理しました。"
                        )
                        if stop_reason == "tool_failed"
                        else (
                            "\n一部の観点は未取得です。取得できた範囲で評価しました。"
                        )
                        if stop_reason
                        in {"incomplete_perspectives", "incomplete_requested_analysis"}
                        else (
                            f"\n追加調査はアプリ側の{reason_label}で停止しました。"
                            "これはGA4 APIの利用上限を示すものではありません。"
                        )
                    )
                display_errors = [
                    error
                    for error in errors
                    if (
                        not error.startswith("requested_analysis_not_observed:")
                        or missing_dimensions
                    )
                    and (
                        error != "invalid_schema_or_generated_numbers"
                        or unresolved_plan_error
                    )
                    and (
                        not error.startswith("followup_context_changed:") or incomplete
                    )
                ]
                if display_errors:
                    body += "\n\n## 処理状況\n\n" + "\n".join(
                        "- " + text(error_label(code))
                        for code in dict.fromkeys(display_errors)
                    )
                if saved_data_only:
                    body += "\n\n指示に従い、追加のデータ取得は行っていません。"
                if report_generated:
                    known_finding_ids = {fact["id"] for fact in facts}
                    included = [
                        section
                        for section in workspace.sections
                        if known_finding_ids.intersection(section.finding_ids)
                    ][-11:]
                    scopes = "\n".join(
                        f"- {cell(section.title)}"
                        + (
                            f"（{section.period.start}〜{section.period.end}）"
                            if section.period
                            else ""
                        )
                        + (
                            f"｜比較 {section.comparison_period.start}"
                            f"〜{section.comparison_period.end}"
                            if section.comparison_period
                            else ""
                        )
                        for section in included
                    )
                    scope_block = f"## 対象・集計条件\n\n{scopes}\n\n" if scopes else ""
                    body = (
                        f"# 分析レポート｜{cell(scope.source_set.name)}\n\n"
                        f"サイト設定バージョン: {site.revision}\n\n"
                        + scope_block
                        + body
                        + "\n\n## レポートの範囲\n\n"
                        "記載した集計・根拠で確認できる範囲です。施策の効果や事業目標の達成とは区別してください。"
                    )
                elif workspace.report_requested:
                    body += "\n\n利用できる根拠がないため、レポートは保存していません。"
                # A grant revoked during text generation must not release a
                # newly composed answer or commit it without reauthorization.
                await self.session(user_id, session_id)
                for hit in retrieval["hits"]:
                    await self.session(user_id, hit["session_id"])
                if staged_answer and answer_plan:
                    await self.session(user_id, answer_source.id)
                    await self.validate_staged(answer_source, answer_plan)
                # Store provenance references, not copies of another session's
                # findings. History restoration reauthorizes each source again.
                result_references: tuple[AnalysisResultReference, ...] = ()
                if saved_data_only:
                    known = {f["id"] for f in facts}
                    candidates = [(session.id, s) for s in workspace.sections] + [
                        (h["session_id"], ReportSection.model_validate(h["section"]))
                        for h in historical_context
                    ]
                    result_references = tuple(
                        dict.fromkeys(
                            AnalysisResultReference(
                                session_id=sid,
                                message_id=s.message_id,
                                source_fingerprint=hashlib.sha256(
                                    str(scope.source_set.ga4).encode()
                                ).hexdigest(),
                                semantic_version=session.state.semantic_version,
                            )
                            for sid, s in candidates
                            if s.finding_ids and set(s.finding_ids).issubset(known)
                        )
                    )[:11]
                assistant = message.model_copy(
                    update={
                        "id": "msg_"
                        + record_id("answer", session.id, operation_id)[:32],
                        "role": MessageRole.ASSISTANT,
                        "content": bounded_document(body),
                        "content_format": "markdown",
                        "result_references": result_references,
                        "finding_ids": tuple(f["id"] for f in facts),
                        "evidence_ids": tuple(
                            dict.fromkeys(
                                eid for f in facts for eid in f["evidence_ids"]
                            )
                        ),
                        "created_at": utc_now(),
                    }
                )
                updated = session.model_copy(
                    update={
                        "revision": session.revision + 1,
                        "state": restore_query_context(session.state, review_anchor)
                        if review_anchor
                        else session.state,
                        "updated_at": utc_now(),
                        "title": (scope.source_set.name + "｜" + content)[:100]
                        if session.title == "新しい分析"
                        else session.title,
                    }
                )
                before_completion_revision = workspace.revision
                workspace = await self.checkpoint(
                    updated,
                    workspace,
                    prepare_only=True,
                    status=final_status,
                    report_message_id=assistant.id
                    if report_generated
                    else workspace.report_message_id,
                    staged_plan=workspace.staged_plan.model_copy(
                        update={"phase": "complete"}
                    )
                    if staged_answer
                    and workspace.staged_plan
                    and workspace.staged_plan.phase == "synthesis"
                    and summary
                    and not missing_dimensions
                    else workspace.staged_plan,
                    memory=workspace.memory.model_copy(
                        update={
                            "verified_finding_ids": tuple(f["id"] for f in facts)[-32:],
                            "pending_tasks": tuple(missing_dimensions)[:16]
                            if staged_active
                            else (
                                decision.remaining_tasks
                                if decision
                                else workspace.memory.pending_tasks
                            ),
                            "entries": tuple(
                                (
                                    *workspace.memory.entries,
                                    *(
                                        MemoryEntry(
                                            text=p.hypothesis[:1000],
                                            kind="hypothesis",
                                            origin="agent_hypothesis",
                                            message_id=assistant.id,
                                        )
                                        for p in (
                                            decision.proposals if decision else ()
                                        )
                                    ),
                                    *(
                                        MemoryEntry(
                                            text=q[:1000],
                                            kind="question",
                                            message_id=assistant.id,
                                            origin="agent_hypothesis",
                                        )
                                        for q in (summary.questions if summary else ())
                                    ),
                                )
                            )[-40:],
                        }
                    ),
                    execution_errors=tuple(errors[-8:]),
                    stop_reason=stop_reason,
                    questions=summary.questions
                    if summary
                    else decision.questions
                    if decision
                    else (),
                    lease_until=None,
                    proposals=decision.proposals
                    if decision and decision.proposals
                    else workspace.proposals,
                    proposals_site_revision=site.revision
                    if decision and decision.proposals
                    else workspace.proposals_site_revision,
                    turns=(
                        *workspace.turns[-19:],
                        {"role": "assistant", "content": body[:4000]},
                    ),
                )
                if not await self.repository.commit_analysis(
                    updated, assistant, (), expected_revision=session.revision
                ):
                    raise AnalysisOrchestrationError("agent_session_conflict")
                await self._save_workspace(
                    updated, before_completion_revision, workspace
                )
                await self.audit(
                    user_id,
                    session.data_source_set_id,
                    "agent.turn",
                    final_status,
                    request_id=operation_id,
                    queries=workspace.completed_queries,
                    sections=len(workspace.sections),
                    stop_reason=stop_reason,
                    execution_errors=errors[-8:],
                )
                yield {"event": "done", "data": workspace.model_dump(mode="json")}
        except (Exception, asyncio.CancelledError) as error:
            code = (
                error.code
                if isinstance(error, AnalysisOrchestrationError)
                else "access_denied"
                if isinstance(error, AccessDenied)
                else "agent_interrupted"
                if isinstance(error, asyncio.CancelledError)
                else "agent_timeout"
                if isinstance(error, TimeoutError)
                else "agent_execution_failed"
            )
            logging.getLogger(__name__).warning(
                "Agent turn failed",
                extra={
                    "error_code": code,
                    "error_type": type(error).__name__,
                    "request_id": operation_id,
                },
            )
            persisted_record = await asyncio.shield(
                self.store.get("workspace", record_id(user_id, session_id))
            )
            if persisted_record is None:
                raise AnalysisOrchestrationError("agent_revision_conflict") from error
            self.memory.check_record(
                persisted_record, user_id, session.data_source_set_id
            )
            persisted = AgentWorkspace.model_validate(persisted_record.payload)
            if persisted.operation_id != operation_id:
                raise AnalysisOrchestrationError("agent_revision_conflict") from error
            workspace = persisted
            workspace = await asyncio.shield(
                self.checkpoint(
                    session,
                    workspace,
                    status="failed",
                    lease_until=None,
                    last_error=code,
                    execution_steps=tuple(
                        entry.model_copy(
                            update={"status": "failed", "error_code": code}
                        )
                        if entry.status == "running"
                        else entry
                        for entry in workspace.execution_steps
                    ),
                )
            )
            await asyncio.shield(
                self.audit(
                    user_id,
                    session.data_source_set_id,
                    "agent.turn",
                    "failed",
                    request_id=operation_id,
                    error=code,
                )
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            yield {"event": "error", "data": {"code": code}}
