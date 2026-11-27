"""Read-only restoration of cited dependent plans, never inferred dependencies.

Persisted citations nominate sessions independently of lexical search ranking.
A saved plan and all completed steps must still be authorized and verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from analytics_ai.agent.facts import finding_context
from analytics_ai.agent.models import AgentRecord, AgentWorkspace, ReportSection
from analytics_ai.agent.staged_models import StagedPlan
from analytics_ai.agent.store import AgentStore, record_id
from analytics_ai.authorization.service import AccessDenied, AuthorizationService
from analytics_ai.domain.models import AnalysisFilter, AnalysisSession, DatePeriod
from analytics_ai.repositories.protocols import ApplicationRepository


@dataclass(frozen=True, slots=True)
class RecoveredStaged:
    source_session: AnalysisSession
    plan: StagedPlan
    hits: tuple[dict[str, Any], ...]


MAX_CITATIONS = 32
MAX_SOURCE_SESSIONS = 32
MAX_LEGACY_EPISODES = 500


async def _cited_sources(
    repository: ApplicationRepository,
    store: AgentStore,
    session: AnalysisSession,
    evidence_ids: tuple[str, ...],
    finding_ids: tuple[str, ...],
    episode_fingerprint: str,
) -> tuple[list[str], list[AgentRecord] | None] | None:
    """Resolve server-saved citations, never model/client-supplied provenance.

    Findings provide direct lineage for modern answers. Legacy evidence-only
    answers use a bounded exact-ID scan, not the ranked search result window.
    Nominations are not facts: recover_staged reauthorizes and verifies them.
    """
    cited = set(evidence_ids)
    if len(cited) > MAX_CITATIONS or len(set(finding_ids)) > MAX_CITATIONS:
        return None
    sessions: list[str] = []
    records: list[AgentRecord] | None = None
    if finding_ids:
        for fid in dict.fromkeys(finding_ids):
            finding = await repository.get_finding(fid)
            if (
                not finding
                or (
                    finding.user_id,
                    finding.company_id,
                    finding.data_source_set_id,
                    finding.semantic_version,
                )
                != (
                    session.user_id,
                    session.company_id,
                    session.data_source_set_id,
                    session.state.semantic_version,
                )
                or not set(finding.evidence_ids).issubset(cited)
            ):
                # Broken direct lineage must not fall back to a similar report.
                return None
            sessions.append(finding.session_id)
    elif cited:
        records, _ = await store.list_scope(
            "episode", session.user_id, session.data_source_set_id, MAX_LEGACY_EPISODES
        )
        for record in records:
            payload = record.payload
            sid = payload.get("session_id")
            if (
                (record.owner_id, record.data_source_set_id)
                != (session.user_id, session.data_source_set_id)
                or not isinstance(sid, str)
                or payload.get("source_fingerprint") != episode_fingerprint
                or payload.get("semantic_version") != session.state.semantic_version
            ):
                continue
            try:
                section = ReportSection.model_validate(payload["section"])
            except (ValidationError, KeyError):
                continue
            if record.id == record_id(
                session.user_id, sid, section.message_id
            ) and cited.intersection(section.evidence_ids):
                sessions.append(sid)
    sessions = list(dict.fromkeys(sessions))
    if len(sessions) > MAX_SOURCE_SESSIONS:
        return None
    return sessions, records


def _filters(filters: tuple[AnalysisFilter, ...]) -> set[tuple[str, str, str]]:
    return {(f.field_name, f.operator, f.value) for f in filters}


def _numbers(rows: list[tuple[str, str]]) -> dict[str, Decimal] | None:
    try:
        values = {key: Decimal(value) for key, value in rows}
        return (
            values
            if len(values) == len(rows) and all(v.is_finite() for v in values.values())
            else None
        )
    except (InvalidOperation, ValueError):
        return None


async def _verified_hit(
    repository: ApplicationRepository,
    source: AnalysisSession,
    section: ReportSection,
    plan: StagedPlan,
    property_id: str,
    *,
    field: str,
    expected_rows: list[tuple[str, str]],
    expected_filters: set[tuple[str, str, str]],
    expected_evidence: tuple[str, ...],
) -> dict[str, Any] | None:
    if (
        not section.finding_ids
        or not expected_evidence
        or set(section.evidence_ids) != set(expected_evidence)
        or section.dimension_fields != (field,)
        or section.metric_fields != (plan.metric_field,)
        or section.period != plan.anchor.period
        or section.comparison_period is not None
        or _filters(section.filters) != expected_filters
    ):
        return None
    facts: list[dict[str, Any]] = []
    observations: tuple[dict[str, str], ...] = ()
    expected = _numbers(expected_rows)
    if expected is None:
        return None
    for fid in section.finding_ids:
        finding = await repository.get_finding(fid)
        if not finding or (
            finding.user_id,
            finding.session_id,
            finding.company_id,
            finding.data_source_set_id,
            finding.semantic_version,
            finding.metric_field,
            finding.dimension_fields,
        ) != (
            source.user_id,
            source.id,
            source.company_id,
            source.data_source_set_id,
            plan.anchor.semantic_version,
            plan.metric_field,
            (field,),
        ):
            return None
        if set(finding.evidence_ids) != set(expected_evidence):
            return None
        for eid in finding.evidence_ids:
            evidence = await repository.get_evidence(eid)
            if not evidence or (
                evidence.company_id,
                evidence.data_source_set_id,
                evidence.dataset_id,
                evidence.semantic_version,
                evidence.ga4_property_id,
                evidence.query_hash,
                evidence.source,
            ) != (
                source.company_id,
                source.data_source_set_id,
                finding.dataset_id,
                plan.anchor.semantic_version,
                property_id,
                finding.query_hash,
                "ga4",
            ):
                return None
        cells = finding.category_values
        if any(set(cell.dimensions) != {field} for cell in cells):
            return None
        if (
            _numbers([(cell.dimensions[field], cell.value) for cell in cells])
            != expected
        ):
            return None
        observations = tuple(
            {field: cell.dimensions[field], plan.metric_field: cell.value}
            for cell in cells
        )
        facts.append(
            {
                **finding_context(finding, list(finding.evidence_ids)),
                "period": section.period.model_dump(mode="json")
                if section.period
                else None,
                "comparison_period": None,
                "filters": [f.model_dump(mode="json") for f in section.filters],
                "historical": True,
                "recorded_at": finding.created_at.isoformat(),
                "source_session_id": source.id,
            }
        )
    return {
        "session_id": source.id,
        "question": plan.objective,
        "recorded_at": facts[0]["recorded_at"],
        "semantic_version": plan.anchor.semantic_version,
        "historical": True,
        "facts": facts,
        "section": section.model_copy(update={"observations": observations}).model_dump(
            mode="json"
        ),
    }


async def recover_staged(
    repository: ApplicationRepository,
    store: AgentStore,
    current_session: AnalysisSession,
    hits: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    preferred_evidence_ids: tuple[str, ...],
    preferred_finding_ids: tuple[str, ...] = (),
    required_dimension_fields: tuple[str, ...] = (),
    required_metric_fields: tuple[str, ...] = (),
    required_period: DatePeriod | None = None,
    required_comparison_period: DatePeriod | None = None,
    required_filters: tuple[AnalysisFilter, ...] | None = None,
) -> RecoveredStaged | None:
    """Restore one unambiguous cited saved plan without querying or mutating.

    Cross-session restoration needs a server-observed citation, not just textual
    similarity. Unspecified conditions inherit the cited plan for this answer only.
    Missing completed-step evidence invalidates restoration; pending steps stay so.
    """
    authorization = AuthorizationService(repository)
    scope = await authorization.authorize_session(
        current_session.user_id, current_session
    )
    if not scope.source_set.ga4 or required_comparison_period is not None:
        return None
    fingerprint = record_id(str(scope.source_set.ga4))
    episode_fingerprint = record_id(
        scope.source_set.company_id, str(scope.source_set.ga4)
    )
    archived: list[AgentRecord] | None = None
    if preferred_evidence_ids or preferred_finding_ids:
        cited_sources = await _cited_sources(
            repository,
            store,
            current_session,
            preferred_evidence_ids,
            preferred_finding_ids,
            episode_fingerprint,
        )
        if cited_sources is None:
            return None
        session_ids, archived = cited_sources
    else:
        session_ids = list(
            dict.fromkeys(
                str(hit["session_id"])
                for hit in hits[:MAX_SOURCE_SESSIONS]
                if isinstance(hit.get("session_id"), str)
            )
        )
    candidates: list[tuple[int, AnalysisSession, AgentWorkspace, StagedPlan]] = []
    for sid in session_ids:
        source = await repository.get_session(sid)
        if not source or (
            source.user_id,
            source.company_id,
            source.data_source_set_id,
            source.state.semantic_version,
        ) != (
            current_session.user_id,
            current_session.company_id,
            current_session.data_source_set_id,
            current_session.state.semantic_version,
        ):
            continue
        try:
            await authorization.authorize_session(current_session.user_id, source)
        except AccessDenied:
            continue
        record = await store.get("workspace", record_id(source.user_id, source.id))
        if not record or (record.owner_id, record.data_source_set_id) != (
            source.user_id,
            source.data_source_set_id,
        ):
            continue
        try:
            workspace = AgentWorkspace.model_validate(record.payload)
        except ValidationError:
            continue
        plan = workspace.staged_plan
        if (
            plan is None
            or plan.phase == "rank"
            or not plan.rank_evidence_ids
            or plan.source_fingerprint != fingerprint
            or plan.anchor.data_source_set_id != current_session.data_source_set_id
            or plan.anchor.semantic_version != current_session.state.semantic_version
            or plan.anchor.period is None
            or plan.anchor.comparison_period is not None
            or not set(required_dimension_fields).issubset(
                {plan.primary_field, plan.secondary_field}
            )
            or not set(required_metric_fields).issubset({plan.metric_field})
            or (required_period is not None and required_period != plan.anchor.period)
            or (
                required_filters is not None
                and _filters(required_filters) != _filters(plan.anchor.filters)
            )
        ):
            continue
        evidence = {
            *plan.rank_evidence_ids,
            *(eid for item in plan.items for eid in item.evidence_ids),
        }
        overlap = len(evidence.intersection(preferred_evidence_ids))
        if source.id != current_session.id and not overlap:
            continue
        candidates.append((overlap, source, workspace, plan))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    _, source, workspace, plan = candidates[0]
    sections = list(workspace.sections)
    # Reuse the legacy scan when needed; do not double its read budget.
    # Trimmed sections must still match the exact plan and all evidence.
    if archived is None:
        archived, _ = await store.list_scope(
            "episode", source.user_id, source.data_source_set_id, MAX_LEGACY_EPISODES
        )
    records = archived
    for record in records:
        payload = record.payload
        if (
            record.owner_id != source.user_id
            or record.data_source_set_id != source.data_source_set_id
            or payload.get("session_id") != source.id
            or payload.get("source_fingerprint") != episode_fingerprint
            or payload.get("semantic_version") != plan.anchor.semantic_version
        ):
            continue
        try:
            section = ReportSection.model_validate(payload["section"])
        except (ValidationError, KeyError):
            continue
        if record.id != record_id(source.user_id, source.id, section.message_id):
            continue
        if not any(s.message_id == section.message_id for s in sections):
            sections.append(section)
    expected_steps = [
        (
            plan.primary_field,
            [(item.value, item.total) for item in plan.items],
            _filters(plan.anchor.filters),
            plan.rank_evidence_ids,
        ),
        *(
            (
                plan.secondary_field,
                [
                    (row[plan.secondary_field], row[plan.metric_field])
                    for row in item.rows
                ],
                {f for f in _filters(plan.anchor.filters) if f[0] != plan.primary_field}
                | {(plan.primary_field, "exact", item.value)},
                item.evidence_ids,
            )
            for item in plan.items
            if item.status == "complete"
        ),
    ]
    verified: list[dict[str, Any]] = []
    for field, rows, filters, evidence_ids in expected_steps:
        found = None
        for section in sections:
            if set(section.evidence_ids) != set(evidence_ids):
                continue
            found = await _verified_hit(
                repository,
                source,
                section,
                plan,
                scope.source_set.ga4.property_id,
                field=field,
                expected_rows=rows,
                expected_filters=filters,
                expected_evidence=evidence_ids,
            )
            if found:
                break
        if found is None:
            return None
        verified.append(found)
    latest = await authorization.authorize_session(
        current_session.user_id, current_session
    )
    await authorization.authorize_session(current_session.user_id, source)
    if record_id(str(latest.source_set.ga4)) != fingerprint:
        raise AccessDenied("memory_source_changed")
    return RecoveredStaged(source_session=source, plan=plan, hits=tuple(verified))
