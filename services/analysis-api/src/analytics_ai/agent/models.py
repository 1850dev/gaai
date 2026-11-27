"""Contracts for user-owned site context, report workspaces and schedules."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from analytics_ai.agent.memory_models import MemoryQuote, WorkingMemory
from analytics_ai.agent.staged_models import GroupedRequest, StagedPlan
from analytics_ai.domain.models import AnalysisFilter, DatePeriod, FrozenModel, utc_now
from analytics_ai.orchestrator.planning import IntentPlan

Text = Annotated[str, Field(max_length=2000)]


def qualitative_prose(value: str) -> str:
    # GA4 is a product name, not a measured quantity. All result numbers are
    # rendered from validated facts by code, never accepted from model prose.
    checked = value.replace("GA4", "Google Analytics")
    if any(c.isdecimal() for c in checked) or any(c in checked for c in "%％¥￥$€"):
        raise ValueError("use_qualitative_prose_and_fact_ids_not_numbers")
    return value


QualitativeText = Annotated[
    str, Field(max_length=1500), AfterValidator(qualitative_prose)
]


def grounded_prose(value: str, info: ValidationInfo) -> str:
    # Numeric URL segments / source names are labels, not LLM calculations.
    # Only the summary caller supplies these exact, evidence-checked labels.
    # Bare numbers, metric values and invented labels remain disallowed.
    checked = value
    for label in sorted(
        (info.context or {}).get("observed_labels", ()), key=len, reverse=True
    ):
        checked = checked.replace(label, "観測済みラベル")
    qualitative_prose(checked)
    return value


GroundedText = Annotated[str, Field(max_length=1500), AfterValidator(grounded_prose)]


class GroundedAssessment(FrozenModel):
    interpretation: GroundedText
    fact_ids: tuple[str, ...] = Field(min_length=1, max_length=8)


class AgentSummary(FrozenModel):
    """Read-only answer contract: no plan, tool, resource or proposal schema."""

    explanation: GroundedText
    assessments: tuple[GroundedAssessment, ...] = Field(default=(), max_length=6)
    limitations: tuple[GroundedText, ...] = Field(default=(), max_length=4)
    questions: tuple[GroundedText, ...] = Field(default=(), max_length=3)


Surface = Literal[
    "home",
    "listing",
    "detail",
    "content",
    "cart",
    "checkout",
    "payment",
    "speed",
    "advertising",
    "device",
]


class SiteContext(FrozenModel):
    """Descriptive owner/DSS background; legacy field names are not permissions."""

    revision: int = Field(default=0, ge=0)
    site_basics: str = Field(default="", max_length=4000)
    technical_constraints: str = Field(default="", max_length=4000)
    content_policy: str = Field(default="", max_length=4000)
    goals: str = Field(default="", max_length=2000)
    allowed_surfaces: tuple[Surface, ...] = ("home", "listing", "detail", "content")
    allow_new_content: bool = True
    allow_rewrite: bool = True
    updated_at: datetime = Field(default_factory=utc_now)


class PropertyPreferences(FrozenModel):
    revision: int = Field(default=0, ge=0)
    hidden_property_ids: tuple[Annotated[str, Field(pattern=r"^[0-9]{1,20}$")], ...] = (
        Field(default=(), max_length=500)
    )


class GoalCheck(FrozenModel):
    """A proposed evaluation path, never proof of goal attainment or a tool call."""

    goal: str = Field(min_length=1, max_length=500)
    goal_basis: Literal[
        "user_request", "site_context", "user_memory", "unconfirmed"
    ] = "unconfirmed"
    goal_quote: str | None = Field(default=None, min_length=1, max_length=500)
    required_evidence: str = Field(min_length=1, max_length=600)
    evidence_status: Literal["proxy_only", "unmeasured", "goal_unconfirmed"]
    gap: str = Field(min_length=1, max_length=600)
    next_check: str = Field(min_length=1, max_length=600)
    next_check_kind: Literal[
        "registered_analysis", "measurement_check", "business_question", "human_review"
    ]


class ProposalEvaluation(FrozenModel):
    """Why and how to assess a hypothesis, not a site permission policy."""

    goal_alignment: str = Field(min_length=1, max_length=800)
    metric_reason: str = Field(min_length=1, max_length=800)
    metric_limitations: str = Field(min_length=1, max_length=800)
    measurement_status: Literal["proxy", "needs_confirmation"]
    prerequisites: tuple[Annotated[str, Field(max_length=500)], ...] = Field(
        default=(), max_length=4
    )
    goal_checks: tuple[GoalCheck, ...] = Field(default=(), max_length=3)


class Proposal(FrozenModel):
    title: str = Field(min_length=1, max_length=160)
    hypothesis: Text
    changes: Text
    surfaces: tuple[Surface, ...] = Field(min_length=1, max_length=10)
    content_action: Literal["none", "new", "rewrite"] = "none"
    suggested_title: str = Field(default="", max_length=240)
    suggested_description: Text = ""
    keywords: tuple[Annotated[str, Field(max_length=80)], ...] = Field(
        default=(), max_length=10
    )
    validation_plan: Text
    success_metric: str | None = Field(default=None, min_length=1, max_length=100)
    evaluation: ProposalEvaluation | None = None
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    priority: Literal["high", "medium", "low"] = "medium"


class ObservedFilter(FrozenModel):
    field: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_:]{0,127}$")
    value: str = Field(min_length=1, max_length=300)


class AgentDecision(FrozenModel):
    memory_quotes: tuple[MemoryQuote, ...] = Field(default=(), max_length=6)
    grouped_request: GroupedRequest | None = None
    action: Literal["ask", "query", "finish"]
    objective: str = Field(max_length=500)
    explanation: str = Field(min_length=1, max_length=1500)
    questions: tuple[Annotated[str, Field(max_length=500)], ...] = Field(
        default=(), max_length=3
    )
    plan: IntentPlan | None = None
    observed_filters: tuple[ObservedFilter, ...] = Field(default=(), max_length=4)
    proposals: tuple[Proposal, ...] = Field(default=(), max_length=5)
    open_questions: tuple[Annotated[str, Field(max_length=500)], ...] = Field(
        default=(), max_length=8
    )
    workflow_id: str | None = None
    remaining_tasks: tuple[str, ...] = Field(default=(), max_length=4)
    assumptions: tuple[str, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def valid_action(self) -> AgentDecision:
        if self.action == "query" and self.plan is None:
            raise ValueError("agent_query_requires_plan")
        if self.action != "query" and self.plan is not None:
            raise ValueError("agent_non_query_cannot_have_plan")
        if self.action == "ask" and not self.questions:
            raise ValueError("agent_ask_requires_questions")
        # Query explanations are planning metadata, never displayed as findings.
        # Do not discard an otherwise valid query for mentioning its year.
        if self.action != "query":
            qualitative_prose(self.explanation)
        return self


class ReportSection(FrozenModel):
    title: str = Field(max_length=200)
    message_id: str
    finding_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    facts: tuple[str, ...] = ()
    quality_notes: tuple[str, ...] = ()
    observations: tuple[dict[str, str | int | float | None], ...] = ()
    dimension_fields: tuple[str, ...] = ()
    metric_fields: tuple[str, ...] = ()
    observed_periods: tuple[str, ...] = ()
    period: DatePeriod | None = None
    perspective: str | None = None
    comparison_period: DatePeriod | None = None
    filters: tuple[AnalysisFilter, ...] = ()
    operation_id: str = ""


class AgentExecutionStep(FrozenModel):
    id: str
    tool: Literal["ga4.analyze"] = "ga4.analyze"
    plan: IntentPlan
    status: Literal["running", "succeeded", "failed"]
    evidence_ids: tuple[str, ...] = ()
    error_code: str | None = None


class AgentWorkspace(FrozenModel):
    memory: WorkingMemory = Field(default_factory=WorkingMemory)
    staged_plan: StagedPlan | None = None
    required_dimension_groups: tuple[tuple[str, ...], ...] = ()
    # Server-selected, reauthorized references for discussing an existing result.
    # Never accepted from model output or a client mutation.
    reused_section_ids: tuple[str, ...] = ()
    required_dimension_fields: tuple[str, ...] = ()
    required_metric_fields: tuple[str, ...] = ()
    required_period: DatePeriod | None = None
    required_comparison_period: DatePeriod | None = None
    # Distinguish explicit no-comparison from unconstrained historical requests.
    comparison_required_exactly: bool = False
    required_filters: tuple[AnalysisFilter, ...] | None = None
    workflow_id: str | None = None
    remaining_tasks: tuple[str, ...] = ()
    execution_steps: tuple[AgentExecutionStep, ...] = ()
    report_requested: bool = False
    # Points to an immutable, authorized Markdown chat message. Do not store a
    # second copy of the body in the mutable workspace.
    report_message_id: str | None = Field(default=None, pattern=r"^msg_[a-f0-9]{32}$")
    execution_errors: tuple[str, ...] = ()
    stop_reason: str | None = None
    review_targets: tuple[str, ...] = ()
    completed_perspectives: tuple[str, ...] = ()
    review_assumptions: tuple[str, ...] = ()
    revision: int = 0
    objective: str = ""
    status: Literal["ready", "running", "waiting", "complete", "partial", "failed"] = (
        "ready"
    )
    operation_id: str = ""
    lease_until: datetime | None = None
    context_revision: int = 0
    proposals_site_revision: int | None = None
    turns: tuple[dict[str, str], ...] = ()
    sections: tuple[ReportSection, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    questions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    completed_queries: int = 0
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ReportSchedule(FrozenModel):
    id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")
    revision: int = Field(default=0, ge=0)
    data_source_set_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)
    frequency: Literal["weekly", "monthly"] = "weekly"
    weekday: int = Field(default=0, ge=0, le=6)
    monthday: int = Field(default=1, ge=1, le=28)
    hour: int = Field(default=9, ge=0, le=23)
    timezone: str = "Asia/Tokyo"
    enabled: bool = False
    next_run_at: datetime | None = None
    last_session_id: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    running_until: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("invalid_schedule_timezone") from error
        return value


class AgentRecord(FrozenModel):
    id: str
    kind: Literal[
        "site",
        "preferences",
        "workspace",
        "schedule",
        "agreements",
        "episode",
        "operation",
    ]
    owner_id: str
    data_source_set_id: str = ""
    revision: int
    payload: dict[str, Any]
    due_at: datetime | None = None
