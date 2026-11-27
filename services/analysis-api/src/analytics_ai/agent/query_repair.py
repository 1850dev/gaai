"""Allow one bounded, scope-preserving retry of a failed browser GA4 query."""

from dataclasses import dataclass
from typing import Any

from analytics_ai.agent.models import AgentWorkspace
from analytics_ai.analysis_state.reducer import AnalysisStateReducer
from analytics_ai.domain.models import AnalysisState
from analytics_ai.orchestrator.planning import CompiledPlan

RECOVERABLE_GA4_ERRORS = frozenset(
    {"ga4_metric_dimension_incompatible", "mcp_result_row_limit_exceeded"}
)


@dataclass(frozen=True)
class QueryRepair:
    code: str
    previous: CompiledPlan
    state: AnalysisState
    fixed_row_count: bool

    def context(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "remaining_retries": 1,
            "dimensions": [d.label for d in self.previous.dimensions],
            "metrics": [m.label for m in self.previous.metrics],
            "row_limit": self.previous.arguments.get("limit"),
            "period": self.state.period.model_dump(mode="json")
            if self.state.period
            else None,
            "comparison_period": self.state.comparison_period.model_dump(mode="json")
            if self.state.comparison_period
            else None,
        }

    def accepts(
        self, proposed: CompiledPlan, state: AnalysisState, workspace: AgentWorkspace
    ) -> bool:
        candidate = AnalysisStateReducer().apply(state, proposed.delta).state
        if (
            proposed.route != self.previous.route
            or proposed.tool_name != self.previous.tool_name
            or proposed.tool_name != "run_report"
            or candidate.period != self.state.period
            or candidate.comparison_period != self.state.comparison_period
            or tuple(candidate.filters) != tuple(self.state.filters)
            or candidate.time_grain != self.state.time_grain
            or proposed.preferred_chart_type != self.previous.preferred_chart_type
            or any(
                self.previous.arguments.get(key) != proposed.arguments.get(key)
                for key in ("dimension_filter", "metric_filter")
            )
        ):
            return False
        axes = {item.field for item in proposed.dimensions}
        metrics = {item.field for item in proposed.metrics}
        if not metrics or not (
            set(workspace.required_dimension_fields).issubset(axes)
            and all(
                set(group).issubset(axes)
                for group in workspace.required_dimension_groups
            )
            and set(workspace.required_metric_fields).issubset(metrics)
        ):
            return False
        if self.code == "mcp_result_row_limit_exceeded":
            if self.fixed_row_count:
                return False
            old_limit = self.previous.arguments.get("limit")
            new_limit = proposed.arguments.get("limit")
            old_order = self.previous.arguments.get("order_bys")
            # An unordered result may gain an implicit order with a smaller
            # cap. An existing order is part of the plan and cannot change.
            bounds = {"limit", "order_bys"}
            return (
                type(old_limit) is int
                and type(new_limit) is int
                and 0 < new_limit < old_limit
                and (
                    old_order is None
                    or old_order == proposed.arguments.get("order_bys")
                )
                and {
                    k: v for k, v in self.previous.arguments.items() if k not in bounds
                }
                == {k: v for k, v in proposed.arguments.items() if k not in bounds}
            )
        if self.code != "ga4_metric_dimension_incompatible":
            return False
        old_axes = {item.field for item in self.previous.dimensions}
        old_metrics = {item.field for item in self.previous.metrics}
        return (
            axes.issubset(old_axes)
            and metrics.issubset(old_metrics)
            and bool(self.previous.metrics)
            and self.previous.metrics[0].field in metrics
            and (axes != old_axes or metrics != old_metrics)
            and proposed.arguments.get("limit") == self.previous.arguments.get("limit")
        )
