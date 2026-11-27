"""Small, server-owned conversation context; never a substitute for authorization."""

from __future__ import annotations

import re
from typing import Any

from analytics_ai.agent.models import AgentWorkspace
from analytics_ai.domain.models import AnalysisFilter, AnalysisState, SemanticDefinition
from analytics_ai.orchestrator.planning import PlanCompiler, VertexIntentPlanner
from analytics_ai.semantic.resolver import normalize_term

QUERY_CONTEXT_FIELDS = frozenset(
    {
        "period",
        "comparison_period",
        "focus",
        "metrics",
        "dimensions",
        "filters",
        "time_grain",
        "selected_skill",
        "skill_version",
    }
)


def query_context(state: AnalysisState) -> dict[str, Any]:
    """Do not repeat the ever-growing finding texts or routing history in a plan."""
    return state.model_dump(
        mode="json", include=set(QUERY_CONTEXT_FIELDS) | {"semantic_version"}
    )


def restore_query_context(
    latest: AnalysisState, anchor: AnalysisState
) -> AnalysisState:
    """Restore a review's primary conditions, retaining every committed result."""
    if (
        latest.data_source_set_id != anchor.data_source_set_id
        or latest.semantic_version != anchor.semantic_version
    ):
        raise ValueError("conversation_anchor_scope_mismatch")
    return latest.model_copy(
        update={field: getattr(anchor, field) for field in QUERY_CONTEXT_FIELDS}
    )


def planning_workspace(
    workspace: AgentWorkspace, authorized_fact_ids: set[str]
) -> dict[str, Any]:
    """Bound history independently of persisted history size.

    Current question is supplied separately in full. Prior turns support replies
    such as 'yes'; structured conditions and verified observations support actual
    queries. Truncated conversation prose is never a filter-value source.
    """
    result = workspace.model_dump(
        mode="json",
        include={
            "objective",
            "report_requested",
            "questions",
            "open_questions",
            "workflow_id",
            "remaining_tasks",
            "review_targets",
            "completed_perspectives",
            "review_assumptions",
            "completed_queries",
            "reused_section_ids",
            "required_dimension_fields",
            "required_dimension_groups",
            "required_metric_fields",
            "required_period",
            "required_comparison_period",
            "comparison_required_exactly",
            "required_filters",
        },
    )
    prior = workspace.turns[:-1] if workspace.turns else ()
    result["turns"] = [
        {"role": t.get("role", ""), "content": t.get("content", "")[:1000]}
        for t in prior[-4:]
    ]
    result["history_truncated"] = len(prior) > 4 or any(
        len(t.get("content", "")) > 1000 for t in prior[-4:]
    )
    sections = [
        s for s in workspace.sections if set(s.finding_ids) & authorized_fact_ids
    ][-8:]
    result["sections"] = [
        s.model_dump(mode="json", exclude={"facts"}) for s in sections
    ]
    # Detailed plans already live in the audit trace. The controller needs their
    # outcome, not repeated full schemas and arguments from every previous step.
    result["execution_steps"] = [
        {
            "intent": e.plan.intent,
            "status": e.status,
            "evidence_ids": e.evidence_ids,
            "error_code": e.error_code,
        }
        for e in workspace.execution_steps[-4:]
    ]
    result["proposals"] = [p.model_dump(mode="json") for p in workspace.proposals[:3]]
    return result


def existing_result_discussion(
    question: str, definitions: list[SemanticDefinition]
) -> bool:
    """Conservative reuse permission for discussing already retrieved results.

    This does not recognize arbitrary query intent or authorize a tool. Ambiguous
    or changed conditions go through normal planning/cache instead of falsely
    marking older evidence as a completed new analysis.
    """
    if not re.search(
        r"(?:今の|この|直前の|さっきの|先ほどの).{0,40}(?:結果|データ|分析)", question
    ):
        return False
    if not re.search(r"説明|解説|考察|意味|読み取|示唆|要約|まとめ", question):
        return False
    if re.search(
        r"\d|[〇一二三四五六七八九十]+(?:年|月|日)|期間|比較|変更|変え|絞|限定|だけ|のみ|"
        r"除外|解除|再取得|再分析|再計算|更新|昨日|今日|先月|今月|来月|先週|今週|"
        r"前年|今年|上期|下期|半期|日別|週別|月別|年別|日次|週次|月次|年次",
        question,
    ):
        return False
    normalized = normalize_term(question)
    return not any(
        normalize_term(alias) in normalized
        for definition in definitions
        if definition.enabled
        and definition.available
        and definition.kind in {"channel", "device", "filter"}
        for alias in (definition.canonical_name, *definition.aliases)
        if alias
    )


def existing_data_only(question: str) -> bool:
    """An explicit no-fetch instruction is an execution restriction, not a hint.

    The presence of a page count or metric name must not turn an explanation
    request into a new query. Ambiguous business goals still use the planner.
    """
    return bool(
        re.search(
            r"(?:新しい|新たな|追加の?|再)?(?:データ)?取得(?:は|を)?"
            r"(?:せず|しない|不要|なし|禁止|せん)|"
            r"(?:追加|新しい|新たな)調査(?:は|を)?(?:せず|しない|不要|なし)|"
            r"(?:GA4|API)(?:を|は)?(?:呼ばず|呼び出さず|呼び出さない)|"
            r"\b(?:do not|don't)\s+(?:fetch|query|retrieve)\b",
            question,
            re.IGNORECASE,
        )
    )


def reusable_sections(
    workspace: AgentWorkspace, state: AnalysisState, authorized_fact_ids: set[str]
) -> tuple[str, ...]:
    """Exact condition match, independently checked against authorized facts."""
    return tuple(
        s.message_id
        for s in workspace.sections
        if s.operation_id != workspace.operation_id
        and s.evidence_ids
        and s.observations
        and s.finding_ids
        and set(s.finding_ids).issubset(authorized_fact_ids)
        and state.period is not None
        and s.period == state.period
        and s.comparison_period == state.comparison_period
        and set(s.filters) == set(state.filters)
        and (
            state.comparison_period is None
            or {"current", "comparison"}.issubset(s.observed_periods)
        )
    )


def explicit_filter_requirements(
    question: str, state: AnalysisState, definitions: list[SemanticDefinition]
) -> tuple[AnalysisFilter, ...] | None:
    """Known, unambiguous positive narrowing must be evidenced, not just narrated.

    Negation, alternatives and arbitrary values remain the typed planner's job.
    No filter is inferred from general mentions in site advice.
    """
    if not re.search(r"だけ|のみ|限定|絞", question) or re.search(
        r"以外|除外|解除|しない|せず|なく|または|あるいは", question
    ):
        return None
    matches = [
        d
        for d in definitions
        if d.kind in {"channel", "device", "filter"}
        and d.enabled
        and d.available
        and d.version == state.semantic_version
        and any(
            VertexIntentPlanner._question_contains_alias(question, alias)
            for alias in (d.canonical_name, *d.aliases)
        )
    ]
    try:
        requested = [PlanCompiler._state_filter(d) for d in matches]
    except ValueError:
        return None
    if not requested or len({f.field_name for f in requested}) != len(requested):
        return None
    merged = {f.field_name: f for f in state.filters}
    merged.update({f.field_name: f for f in requested})
    return tuple(merged.values())
