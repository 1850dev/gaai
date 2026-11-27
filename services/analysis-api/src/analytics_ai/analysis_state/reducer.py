"""Pure state reducer for conversational continuation."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from analytics_ai.domain.models import (
    AnalysisFilter,
    AnalysisFocus,
    AnalysisState,
    DatePeriod,
    FindingRef,
)


@dataclass(frozen=True, slots=True)
class AnalysisDelta:
    period: DatePeriod | None = None
    clear_period: bool = False
    comparison_period: DatePeriod | None = None
    clear_comparison: bool = False
    metric: str | None = None
    dimension: str | None = None
    channel: str | None = None
    device: str | None = None
    metrics: tuple[str, ...] | None = None
    dimensions: tuple[str, ...] | None = None
    filters: tuple[AnalysisFilter, ...] | None = None
    clear_focus_filters: bool = False
    time_grain: str | None = None
    clear_time_grain: bool = False
    selected_skill: str | None = None
    skill_version: str | None = None
    dataset_ids: tuple[str, ...] = ()
    findings: tuple[FindingRef, ...] = ()
    route: str | None = None
    compare_previous_month: bool = False
    compare_previous_year: bool = False
    compare_previous_period: bool = False
    compare_previous_week: bool = False
    visualization_only: bool = False


@dataclass(frozen=True, slots=True)
class ReductionResult:
    state: AnalysisState
    visualization_only: bool


class AnalysisStateReducer:
    def apply(self, state: AnalysisState, delta: AnalysisDelta) -> ReductionResult:
        period = None if delta.clear_period else delta.period or state.period
        comparison_flags = sum(
            (
                delta.compare_previous_month,
                delta.compare_previous_year,
                delta.compare_previous_period,
                delta.compare_previous_week,
            )
        )
        if comparison_flags > 1:
            raise ValueError("only one comparison period can be requested")
        if delta.comparison_period is not None and comparison_flags:
            raise ValueError("explicit_and_relative_comparison_are_mutually_exclusive")
        if delta.clear_comparison and (
            delta.comparison_period is not None or comparison_flags
        ):
            raise ValueError("clear_comparison_cannot_request_a_comparison")
        # A new primary interval must not silently retain a baseline belonging
        # to the old question. Drilldowns that inherit (or repeat) the same
        # interval retain it; an explicitly supplied baseline always wins.
        primary_changed = delta.clear_period or (
            delta.period is not None and delta.period != state.period
        )
        comparison = (
            None
            if delta.clear_comparison
            else delta.comparison_period
            or (None if primary_changed else state.comparison_period)
        )
        if comparison is not None and period is None:
            raise ValueError("period_is_required_for_explicit_comparison")
        if delta.compare_previous_month:
            if period is None:
                raise ValueError("period is required for previous-month comparison")
            comparison = self.previous_month_period(period)
        if delta.compare_previous_year:
            if period is None:
                raise ValueError("period is required for previous-year comparison")
            comparison = self.previous_year_period(period)
        if delta.compare_previous_period:
            if period is None:
                raise ValueError("period is required for previous-period comparison")
            comparison = self.previous_period(period)
        if delta.compare_previous_week:
            if period is None:
                raise ValueError("period is required for previous-week comparison")
            comparison = self.previous_week_period(period)

        focus = AnalysisFocus(
            metric=(
                (delta.metric or (delta.metrics[0] if delta.metrics else None))
                if delta.metrics is not None
                else (delta.metric or state.focus.metric)
            ),
            dimension=(
                (delta.dimension or (delta.dimensions[0] if delta.dimensions else None))
                if delta.dimensions is not None
                else (delta.dimension or state.focus.dimension)
            ),
            channel=delta.channel
            or (None if delta.clear_focus_filters else state.focus.channel),
            device=delta.device
            or (None if delta.clear_focus_filters else state.focus.device),
        )
        metrics = state.metrics
        if delta.metrics is not None:
            metrics = delta.metrics
        elif not metrics and focus.metric:
            metrics = (focus.metric,)
        dimensions = state.dimensions
        if delta.dimensions is not None:
            dimensions = delta.dimensions
        elif not dimensions and focus.dimension:
            dimensions = (focus.dimension,)
        filters = delta.filters if delta.filters is not None else state.filters
        time_grain = (
            None if delta.clear_time_grain else delta.time_grain or state.time_grain
        )
        datasets = tuple(dict.fromkeys((*state.dataset_ids, *delta.dataset_ids)))
        by_id = {finding.id: finding for finding in state.findings}
        by_id.update({finding.id: finding for finding in delta.findings})
        history = state.router_history
        if delta.route:
            history = (*history, delta.route)
        updated = state.model_copy(
            update={
                "period": period,
                "comparison_period": comparison,
                "focus": focus,
                "metrics": metrics,
                "dimensions": dimensions,
                "filters": filters,
                "time_grain": time_grain,
                "selected_skill": delta.selected_skill or state.selected_skill,
                "skill_version": delta.skill_version or state.skill_version,
                "dataset_ids": datasets,
                "findings": tuple(by_id.values()),
                "router_history": history,
            }
        )
        return ReductionResult(updated, delta.visualization_only)

    @staticmethod
    def previous_month_period(period: DatePeriod) -> DatePeriod:
        def previous_month(value: date) -> date:
            year = value.year if value.month > 1 else value.year - 1
            month = value.month - 1 if value.month > 1 else 12
            last_day = calendar.monthrange(year, month)[1]
            return value.replace(
                year=year,
                month=month,
                day=min(value.day, last_day),
            )

        return DatePeriod(
            start=previous_month(period.start),
            end=previous_month(period.end),
        )

    @staticmethod
    def previous_year_period(period: DatePeriod) -> DatePeriod:
        def previous_year(value: date) -> date:
            year = value.year - 1
            last_day = calendar.monthrange(year, value.month)[1]
            return value.replace(year=year, day=min(value.day, last_day))

        return DatePeriod(
            start=previous_year(period.start),
            end=previous_year(period.end),
        )

    @staticmethod
    def previous_period(period: DatePeriod) -> DatePeriod:
        duration = (period.end - period.start).days + 1
        return DatePeriod(
            start=period.start - timedelta(days=duration),
            end=period.start - timedelta(days=1),
        )

    @staticmethod
    def previous_week_period(period: DatePeriod) -> DatePeriod:
        return DatePeriod(
            start=period.start - timedelta(days=7),
            end=period.end - timedelta(days=7),
        )
