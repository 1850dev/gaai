"""Narrow, untrusted browser input. Never deserialize it as an internal plan."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analytics_ai.domain.models import DatePeriod
from analytics_ai.orchestrator.planning import IntentPlan, RankingIntent
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.orchestrator.time_grain import TimeGrain

Term = Annotated[str, Field(min_length=1, max_length=120)]
BrowserSkill = Literal[
    "ga4.kpi_snapshot.v1", "ga4.trend.v1", "ga4.breakdown_rank.v1", "ga4.compare.v1"
]


class BrowserPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: BrowserSkill
    metric: Term
    dimension: Term | None = None
    period: DatePeriod
    comparison_period: DatePeriod | None = None
    grain: TimeGrain | None = None
    limit: int | None = Field(default=None, ge=1, le=25, strict=True)
    presentation: Literal["table"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "BrowserPlan":
        for period in (self.period, self.comparison_period):
            if period and (period.end - period.start).days >= 366:
                raise ValueError("browser_period_too_long")
        if (self.skill_id == "ga4.trend.v1") != (self.grain is not None):
            raise ValueError("trend_requires_grain")
        if (self.skill_id == "ga4.breakdown_rank.v1") != (self.limit is not None):
            raise ValueError("ranking_requires_limit")
        if self.limit and not self.dimension:
            raise ValueError("ranking_requires_dimension")
        if self.dimension and self.skill_id in {"ga4.trend.v1", "ga4.kpi_snapshot.v1"}:
            raise ValueError("unexpected_dimension")
        if (self.skill_id == "ga4.compare.v1") != (self.comparison_period is not None):
            raise ValueError("comparison_requires_two_periods")
        return self

    def to_intent(self) -> IntentPlan:
        return IntentPlan(
            intent="ブラウザで選択・確認したGA4分析",
            skill_id=AnalysisSkillId(self.skill_id),
            metric_terms=(self.metric,),
            dimension_terms=(self.dimension,) if self.dimension else (),
            period=self.period,
            comparison_period=self.comparison_period,
            clear_comparison=self.comparison_period is None,
            time_grain=self.grain,
            ranking=RankingIntent(limit=self.limit) if self.limit else None,
            detail=self.presentation == "table",
            preferred_chart_type=self.presentation,
        )
