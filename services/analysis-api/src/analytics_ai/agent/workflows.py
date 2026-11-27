"""Business workflow skills, distinct from individual GA4 query capabilities.

These contracts guide the agent; they never grant resource access. Only the
existing semantic compiler and authorized gateway can execute ga4.analyze.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from analytics_ai.agent.memory_models import MemoryQuote
from analytics_ai.domain.models import SemanticDefinition
from analytics_ai.orchestrator.time_grain import GRAIN_FIELDS, time_grain_request
from analytics_ai.semantic.resolver import normalize_term

WorkflowId = Literal[
    "analysis.grouped_breakdown.v1",
    "analysis.exploration.v1",
    "analysis.acquisition_comparison.v1",
    "advisory.site_improvement.v1",
]
AgentTool = Literal["ga4.analyze", "user.ask", "evidence.answer"]


class WorkflowSkill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: WorkflowId
    purpose: str
    completion: tuple[str, ...]
    allowed_tools: tuple[AgentTool, ...] = (
        "ga4.analyze",
        "user.ask",
        "evidence.answer",
    )
    max_queries: int = 4
    evidence_policy: str = (
        "Only verified finding IDs; arithmetic by code; missing is not zero; "
        "limited categories are not the whole site; no inferred causation."
    )


WORKFLOW_SKILLS = (
    WorkflowSkill(
        id="analysis.grouped_breakdown.v1",
        purpose=(
            "Rank entities first, then inspect a second dimension for EACH entity; "
            "automatically execute the bounded dependencies within one request."
        ),
        max_queries=11,
        completion=(
            "Single-period one-metric primary ranking, up to ten entities",
            "Exact parent-filtered secondary report for each entity, up to five rows",
            "Persist pending entities; finish with grounded comparisons and hypotheses",
        ),
    ),
    WorkflowSkill(
        id="analysis.exploration.v1",
        purpose="Answer an analytics question; observe before choosing a drilldown.",
        completion=(
            "Answer requested metrics/period/axes or explicitly explain missing data",
        ),
    ),
    WorkflowSkill(
        id="analysis.acquisition_comparison.v1",
        purpose="Compare acquisition between periods, preserving requested axes.",
        completion=(
            "Period-wide metrics for both periods, without summing active users",
            "Same-source/medium or requested channel comparisons; bounded coverage",
            "Evidence-grounded assessment, quality limits and useful next question",
        ),
    ),
    WorkflowSkill(
        id="advisory.site_improvement.v1",
        purpose=(
            "Discuss site improvements using descriptive business context and evidence."
        ),
        completion=(
            "Link site purpose, evidence, hypothesis and measurement rationale",
            "Distinguish proxy metrics from outcomes; disclose unknown measurement",
            "Offer useful hypotheses even when the success metric is unconfirmed",
        ),
    ),
)

TOOL_CATALOG = (
    {
        "id": "ga4.analyze",
        "effect": "read_only_analytics",
        "input": "Semantic terms/periods; no property, credential or raw MCP arguments",
        "output": "Persisted findings, evidence, bounded observations, visualization",
    },
    {
        "id": "user.ask",
        "effect": "conversation_only",
        "input": "Only unresolved information; inherit authorized session context",
        "output": "Wait for the user's next turn; no analytics call",
    },
    {
        "id": "evidence.answer",
        "effect": "conversation_only",
        "input": "Already authorized verified facts in context",
        "output": "Grounded answer, no new analytics; report only on explicit request",
    },
)


class ToolSelection(BaseModel):
    """Small next-action contract; no query grammar or proposal schema here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: AgentTool
    workflow_id: WorkflowId
    objective: str = Field(min_length=1, max_length=500)
    explanation: str = Field(min_length=1, max_length=1500)
    analysis_brief: str = Field(max_length=800)
    remaining_tasks: tuple[str, ...] = Field(max_length=4)
    questions: tuple[str, ...] = Field(max_length=3)
    memory_quotes: tuple[MemoryQuote, ...] = Field(default=(), max_length=6)


def workflow_context() -> dict[str, Any]:
    return {
        "agent_tools": TOOL_CATALOG,
        "workflow_skills": [skill.model_dump(mode="json") for skill in WORKFLOW_SKILLS],
    }


def explicit_dimension_fields(
    question: str, definitions: list[SemanticDefinition]
) -> tuple[str, ...]:
    """Completion check for explicitly named registered axes, not LLM guessing."""
    fields = explicit_semantic_fields(question, definitions, kind="dimension")
    request = time_grain_request(question)
    if request.mentioned:
        # Semantic aliases can also match a negated grouping word. Replace only
        # time-grain fields; keep independently requested category dimensions.
        fields = tuple(field for field in fields if field not in GRAIN_FIELDS.values())
    for grain in () if request.ambiguous else request.requested:
        field = GRAIN_FIELDS[grain]
        if any(
            d.ga4_mapping and d.ga4_mapping.get("dimension") == field
            for d in definitions
            if d.available and d.enabled
        ):
            fields = tuple(dict.fromkeys((*fields, field)))
    return fields


def joint_dimension_fields(
    question: str, definitions: list[SemanticDefinition]
) -> tuple[str, ...]:
    """An explicit cross request needs co-observed axes, not separate reports."""
    if re.search(r"別々|それぞれ独立|個別に|クロス.*不要|掛け合わせ.*不要", question):
        return ()
    fields = explicit_dimension_fields(question, definitions)
    if len(fields) < 2 or not re.search(
        r"掛け|かけ合|クロス|組み合|組合|×|cross|ごとの.+別|別の.+ごと|どの.+どの",
        question,
        re.IGNORECASE,
    ):
        return ()
    return fields


def explicit_semantic_fields(
    question: str, definitions: list[SemanticDefinition], *, kind: str
) -> tuple[str, ...]:
    available = [
        definition
        for definition in definitions
        if definition.available and definition.enabled
    ]
    recognized = literal_term_mentions(
        question,
        (
            (definition.kind, definition.canonical_name, alias)
            for definition in available
            if definition.kind in {"metric", "dimension", "date"}
            for alias in (definition.canonical_name, *definition.aliases)
            if not (definition.kind == "date" and alias == "推移")
        ),
    )
    names = {(term_kind, name) for term_kind, name, _ in recognized}
    return tuple(
        dict.fromkeys(
            str(d.ga4_mapping[kind])
            for d in available
            if (d.kind, d.canonical_name) in names
            and (d.kind == kind or (kind == "dimension" and d.kind == "date"))
            and d.ga4_mapping
            and kind in d.ga4_mapping
        )
    )


def literal_term_mentions(
    question: str, terms: Iterable[tuple[str, str, str]]
) -> tuple[tuple[str, str, str], ...]:
    """Match the longer registered phrase only at the same occurrence.

    'セッションの参照元' is an axis, not a request for the sessions
    metric. An explicit 'セッション数' elsewhere is still a requested metric.
    """
    normalized = normalize_term(question)
    matches: list[tuple[int, int, str, str, str]] = []
    for kind, name, alias in terms:
        text = normalize_term(alias)
        if not text:
            continue
        pattern = re.escape(text)
        if text.isascii():
            pattern = rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"
        matches.extend(
            (match.start(), match.end(), kind, name, alias)
            for match in re.finditer(pattern, normalized)
        )
    return tuple(
        dict.fromkeys(
            (kind, name, alias)
            for start, end, kind, name, alias in sorted(matches)
            if not any(
                a <= start and end <= b and (a, b) != (start, end)
                for a, b, *_ in matches
            )
        )
    )
