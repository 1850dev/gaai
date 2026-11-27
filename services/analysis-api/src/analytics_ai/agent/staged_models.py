"""Validated, persisted dependencies for grouped analysis."""

from typing import Literal

from pydantic import Field

from analytics_ai.domain.models import AnalysisState, FrozenModel
from analytics_ai.orchestrator.planning import IntentPlan


class GroupedRequest(FrozenModel):
    primary: str = Field(min_length=1, max_length=120)
    secondary: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=10, ge=1, le=10)


class GroupItem(FrozenModel):
    value: str = Field(min_length=1, max_length=300)
    total: str
    status: Literal["pending", "complete"] = "pending"
    rows: tuple[dict[str, str], ...] = ()
    evidence_ids: tuple[str, ...] = ()


class StagedPlan(FrozenModel):
    source_fingerprint: str = ""
    version: str = "analysis.grouped_breakdown.v1"
    objective: str
    request: GroupedRequest
    primary_field: str
    secondary_field: str
    metric_field: str
    rank_plan: IntentPlan
    anchor: AnalysisState
    rank_evidence_ids: tuple[str, ...] = ()
    items: tuple[GroupItem, ...] = Field(default=(), max_length=10)
    phase: Literal["rank", "children", "synthesis", "complete"] = "rank"
