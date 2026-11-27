from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from analytics_ai.analysis_state.reducer import AnalysisDelta, AnalysisStateReducer
from analytics_ai.cache.key import cache_key
from analytics_ai.config import Settings
from analytics_ai.domain.models import AnalysisState, DatePeriod
from analytics_ai.orchestrator.planning import (
    ComparisonIntent,
    IntentPlan,
    PlanCompiler,
    VertexIntentPlanner,
    explicit_month_comparison,
)
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_planning import advanced_semantic_definitions, authorized_scope

MAY = DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))
JUNE = DatePeriod(start=date(2026, 6, 1), end=date(2026, 6, 30))
APRIL = DatePeriod(start=date(2026, 4, 1), end=date(2026, 4, 30))
STATE = AnalysisState(data_source_set_id="company_a_web", semantic_version="a-web-v1")


@pytest.mark.parametrize(
    "question",
    [
        "2026年5月と2026年6月を比較した流入元の分析をしてください",
        "2026年6月と2026年5月を比較した流入元の分析をしてください",
        "2026年5月と6月のUUを比較",
    ],
)
def test_named_month_pair_preserves_both_calendar_months(question: str) -> None:
    assert explicit_month_comparison(question) == (JUNE, MAY)


@pytest.mark.parametrize(
    "question",
    [
        "2026年5月の流入元を比較",
        "2026年5月から2026年6月までの流入元を比較",
        "2026年5月1日と2026年6月1日の流入元を比較",
        "2026年5月と2026年6月と2026年7月を比較",
        "2026年5月を2026年6月と比較",
        "2026年5月と2026年6月を基準に比較",
        "2026年5月と2026年6月の前半を比較",
        "2026年5月と2026年6月を比較。後者を基準にして",
        "2026年5月と2026年6月の会計年度を比較",
        "2026年5月と2026年6月は比較なし",
        "2026年5月と2026年5月を比較",
    ],
)
def test_ambiguous_or_directional_periods_defer_to_intent_planner(
    question: str,
) -> None:
    assert explicit_month_comparison(question) is None


def test_named_month_pair_handles_leap_year_and_year_boundary() -> None:
    assert explicit_month_comparison("2023年12月と2024年2月を比較") == (
        DatePeriod(start=date(2024, 2, 1), end=date(2024, 2, 29)),
        DatePeriod(start=date(2023, 12, 1), end=date(2023, 12, 31)),
    )


@pytest.mark.parametrize(
    "kwargs,error",
    [
        (
            {"comparison_period": MAY, "comparison": ComparisonIntent.PREVIOUS_MONTH},
            "explicit_and_relative_comparison_are_mutually_exclusive",
        ),
        (
            {"comparison_period": MAY, "clear_comparison": True},
            "clear_comparison_cannot_request_a_comparison",
        ),
        (
            {"comparison": ComparisonIntent.PREVIOUS_MONTH, "clear_comparison": True},
            "clear_comparison_cannot_request_a_comparison",
        ),
    ],
)
def test_conflicting_comparison_instructions_are_rejected(
    kwargs: dict[str, object], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        IntentPlan.model_validate({"intent": "compare acquisition", **kwargs})


@pytest.mark.asyncio
async def test_explicit_comparison_exact_dates_reach_ga4_and_cache(
    repository: InMemoryRepository,
) -> None:
    compiler = PlanCompiler()
    scope = await authorized_scope(repository)
    plan = IntentPlan(
        intent="compare acquisition",
        period=JUNE,
        comparison_period=MAY,
        metric_terms=("UU", "セッション"),
        dimension_terms=("デバイス",),
    )
    compiled = compiler.compile(
        plan,
        state=STATE,
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )
    assert compiled.skill is not None
    assert compiled.skill.id is AnalysisSkillId.COMPARE
    assert compiled.arguments["date_ranges"] == [
        {"start_date": "2026-06-01", "end_date": "2026-06-30", "name": "current"},
        {"start_date": "2026-05-01", "end_date": "2026-05-31", "name": "comparison"},
    ]
    assert compiled.arguments["metrics"] == ["activeUsers", "sessions"]
    assert compiled.arguments["dimensions"] == ["deviceCategory"]
    assert compiled.descriptor is not None
    assert compiled.descriptor.date_range == ("2026-06-01", "2026-06-30")
    assert compiled.descriptor.comparison_date_range == ("2026-05-01", "2026-05-31")
    assert compiled.descriptor.filters["source_scope"]["property_id"] == "222222222"
    state = AnalysisStateReducer().apply(STATE, compiled.delta).state
    assert state.period == JUNE
    assert state.comparison_period == MAY

    other = compiler.compile(
        plan.model_copy(update={"comparison_period": APRIL}),
        state=STATE,
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )
    assert other.descriptor is not None
    assert cache_key(compiled.descriptor) != cache_key(other.descriptor)

    reworded = compiler.compile(
        plan.model_copy(update={"intent": "another wording for the same request"}),
        state=STATE,
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )
    assert reworded.descriptor is not None
    assert cache_key(compiled.descriptor) == cache_key(reworded.descriptor)


@pytest.mark.asyncio
async def test_comparison_can_reuse_primary_but_not_stale_baseline(
    repository: InMemoryRepository,
) -> None:
    compiler = PlanCompiler()
    scope = await authorized_scope(repository)
    reducer = AnalysisStateReducer()
    state = STATE.model_copy(update={"period": JUNE, "comparison_period": APRIL})
    compiled = compiler.compile(
        IntentPlan(
            intent="compare with May instead",
            comparison_period=MAY,
            metric_terms=("UU",),
        ),
        state=state,
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )
    state = reducer.apply(state, compiled.delta).state
    assert state.period == JUNE
    assert state.comparison_period == MAY
    assert (
        reducer.apply(state, AnalysisDelta(device="mobile")).state.comparison_period
        == MAY
    )
    assert (
        reducer.apply(state, AnalysisDelta(period=JUNE)).state.comparison_period == MAY
    )
    changed = reducer.apply(state, AnalysisDelta(period=APRIL)).state
    assert changed.period == APRIL
    assert changed.comparison_period is None
    assert (
        reducer.apply(
            state, AnalysisDelta(clear_comparison=True)
        ).state.comparison_period
        is None
    )


@pytest.mark.asyncio
async def test_arbitrary_non_adjacent_date_ranges_are_not_normalized_to_months(
    repository: InMemoryRepository,
) -> None:
    primary = DatePeriod(start=date(2026, 6, 12), end=date(2026, 6, 18))
    baseline = DatePeriod(start=date(2025, 11, 5), end=date(2025, 11, 13))
    compiled = PlanCompiler().compile(
        IntentPlan(
            intent="compare campaign windows",
            period=primary,
            comparison_period=baseline,
            metric_terms=("UU",),
        ),
        state=STATE,
        scope=await authorized_scope(repository),
        definitions=advanced_semantic_definitions(),
    )
    assert compiled.arguments["date_ranges"] == [
        {"start_date": "2026-06-12", "end_date": "2026-06-18", "name": "current"},
        {"start_date": "2025-11-05", "end_date": "2025-11-13", "name": "comparison"},
    ]


@pytest.mark.asyncio
async def test_compiler_rechecks_conflicting_copied_plan(
    repository: InMemoryRepository,
) -> None:
    plan = IntentPlan(
        intent="compare acquisition",
        period=JUNE,
        comparison_period=MAY,
        metric_terms=("UU",),
    ).model_copy(update={"comparison": ComparisonIntent.PREVIOUS_MONTH})
    with pytest.raises(ValueError, match="explicit_and_relative_comparison"):
        PlanCompiler().compile(
            plan,
            state=STATE,
            scope=await authorized_scope(repository),
            definitions=advanced_semantic_definitions(),
        )


def test_explicit_comparison_requires_a_primary_interval() -> None:
    with pytest.raises(ValueError, match="period_is_required_for_explicit_comparison"):
        AnalysisStateReducer().apply(STATE, AnalysisDelta(comparison_period=MAY))


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_id", [AnalysisSkillId.REALTIME, AnalysisSkillId.FUNNEL])
async def test_unsupported_skill_cannot_silently_drop_explicit_comparison(
    repository: InMemoryRepository, skill_id: AnalysisSkillId
) -> None:
    with pytest.raises(ValueError, match="analysis_skill_comparison_not_supported"):
        PlanCompiler().compile(
            IntentPlan(
                intent="unsupported compare",
                skill_id=skill_id,
                period=JUNE,
                comparison_period=MAY,
                metric_terms=("UU",),
            ),
            state=STATE,
            scope=await authorized_scope(repository),
            definitions=advanced_semantic_definitions(),
        )


class IncorrectMonthGenerator:
    async def generate(self, prompt: str) -> IntentPlan:
        return IntentPlan(
            intent="compare acquisition",
            period=MAY,
            comparison=ComparisonIntent.PREVIOUS_MONTH,
            metric_terms=("UU",),
        )


@pytest.mark.asyncio
async def test_explicit_month_words_override_model_approximation() -> None:
    planner = VertexIntentPlanner(
        Settings(environment="test"), generator=IncorrectMonthGenerator()
    )
    plan = await planner.plan(
        question="2026年5月と2026年6月を比較した流入元の分析をしてください",
        state=STATE,
        definitions=advanced_semantic_definitions(),
        today=date(2026, 9, 13),
    )
    assert plan.period == JUNE
    assert plan.comparison_period == MAY
    assert plan.comparison is ComparisonIntent.NONE
    assert plan.skill_id is AnalysisSkillId.COMPARE
