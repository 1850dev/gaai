"""Versioned, server-owned GA4 analysis capability contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from analytics_ai.compat import StrEnum


class AnalysisSkillId(StrEnum):
    KPI_SNAPSHOT = "ga4.kpi_snapshot.v1"
    SITE_HEALTH_REPORT = "ga4.site_health_report.v1"
    TREND = "ga4.trend.v1"
    BREAKDOWN_RANK = "ga4.breakdown_rank.v1"
    CROSS_BREAKDOWN = "ga4.cross_breakdown.v1"
    COMPARE = "ga4.compare.v1"
    DRILLDOWN = "ga4.drilldown.v1"
    REALTIME = "ga4.realtime.v1"
    FUNNEL = "ga4.funnel.v1"
    ANOMALY_SCAN = "ga4.anomaly_scan.v1"
    METADATA_DISCOVERY = "ga4.metadata_discovery.v1"


class SkillBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tool_calls: int = Field(ge=1, le=4)
    max_rows: int = Field(ge=1, le=3_000)
    timeout_seconds: int = Field(ge=1, le=180)


class AnalysisSkill(BaseModel):
    """A reviewed capability; the model may select its ID but not its policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: AnalysisSkillId
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    tool_name: str
    min_metrics: int = Field(ge=0, le=8)
    max_metrics: int = Field(ge=0, le=8)
    min_dimensions: int = Field(ge=0, le=4)
    max_dimensions: int = Field(ge=0, le=4)
    requires_period: bool = True
    supports_comparison: bool = False
    cacheable: bool = True
    budget: SkillBudget


class AnalysisSkillRegistry:
    """Immutable application registry used by the trusted compiler."""

    def __init__(self) -> None:
        common = {"version": "1.0.0", "requires_period": True}
        self._skills = {
            AnalysisSkillId.CROSS_BREAKDOWN: AnalysisSkill(
                id=AnalysisSkillId.CROSS_BREAKDOWN,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=4,
                min_dimensions=2,
                max_dimensions=4,
                supports_comparison=True,
                budget=SkillBudget(max_tool_calls=1, max_rows=100, timeout_seconds=45),
                **common,
            ),
            AnalysisSkillId.KPI_SNAPSHOT: AnalysisSkill(
                id=AnalysisSkillId.KPI_SNAPSHOT,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=4,
                min_dimensions=0,
                max_dimensions=0,
                supports_comparison=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=2,
                    timeout_seconds=30,
                ),
                **common,
            ),
            AnalysisSkillId.SITE_HEALTH_REPORT: AnalysisSkill(
                id=AnalysisSkillId.SITE_HEALTH_REPORT,
                tool_name="run_report",
                min_metrics=4,
                max_metrics=4,
                min_dimensions=0,
                max_dimensions=0,
                supports_comparison=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=2,
                    timeout_seconds=30,
                ),
                **common,
            ),
            AnalysisSkillId.TREND: AnalysisSkill(
                id=AnalysisSkillId.TREND,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=4,
                min_dimensions=1,
                max_dimensions=1,
                supports_comparison=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=1_500,
                    timeout_seconds=45,
                ),
                **common,
            ),
            AnalysisSkillId.BREAKDOWN_RANK: AnalysisSkill(
                id=AnalysisSkillId.BREAKDOWN_RANK,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=3,
                min_dimensions=1,
                max_dimensions=2,
                supports_comparison=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=100,
                    timeout_seconds=45,
                ),
                **common,
            ),
            AnalysisSkillId.COMPARE: AnalysisSkill(
                id=AnalysisSkillId.COMPARE,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=4,
                min_dimensions=0,
                max_dimensions=2,
                supports_comparison=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=100,
                    timeout_seconds=45,
                ),
                **common,
            ),
            AnalysisSkillId.DRILLDOWN: AnalysisSkill(
                id=AnalysisSkillId.DRILLDOWN,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=3,
                min_dimensions=0,
                max_dimensions=2,
                supports_comparison=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=100,
                    timeout_seconds=45,
                ),
                **common,
            ),
            AnalysisSkillId.REALTIME: AnalysisSkill(
                id=AnalysisSkillId.REALTIME,
                tool_name="run_realtime_report",
                min_metrics=1,
                max_metrics=3,
                min_dimensions=0,
                max_dimensions=2,
                requires_period=False,
                cacheable=False,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=100,
                    timeout_seconds=20,
                ),
                version="1.0.0",
            ),
            AnalysisSkillId.FUNNEL: AnalysisSkill(
                id=AnalysisSkillId.FUNNEL,
                tool_name="run_funnel_report",
                min_metrics=0,
                max_metrics=0,
                min_dimensions=0,
                max_dimensions=1,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=100,
                    timeout_seconds=60,
                ),
                **common,
            ),
            AnalysisSkillId.ANOMALY_SCAN: AnalysisSkill(
                id=AnalysisSkillId.ANOMALY_SCAN,
                tool_name="run_report",
                min_metrics=1,
                max_metrics=1,
                min_dimensions=1,
                max_dimensions=1,
                supports_comparison=False,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=366,
                    timeout_seconds=45,
                ),
                **common,
            ),
            AnalysisSkillId.METADATA_DISCOVERY: AnalysisSkill(
                id=AnalysisSkillId.METADATA_DISCOVERY,
                tool_name="get_custom_dimensions_and_metrics",
                min_metrics=0,
                max_metrics=0,
                min_dimensions=0,
                max_dimensions=0,
                requires_period=False,
                cacheable=True,
                budget=SkillBudget(
                    max_tool_calls=1,
                    max_rows=500,
                    timeout_seconds=30,
                ),
                version="1.0.0",
            ),
        }

    def get(self, skill_id: AnalysisSkillId) -> AnalysisSkill:
        return self._skills[skill_id]

    def list(self) -> tuple[AnalysisSkill, ...]:
        return tuple(self._skills.values())

    def validate_shape(
        self,
        skill: AnalysisSkill,
        *,
        metric_count: int,
        dimension_count: int,
        has_comparison: bool,
    ) -> None:
        if not skill.min_metrics <= metric_count <= skill.max_metrics:
            raise ValueError("analysis_skill_metric_count_invalid")
        if not skill.min_dimensions <= dimension_count <= skill.max_dimensions:
            raise ValueError("analysis_skill_dimension_count_invalid")
        if has_comparison and not skill.supports_comparison:
            raise ValueError("analysis_skill_comparison_not_supported")


DEFAULT_SKILL_REGISTRY = AnalysisSkillRegistry()
