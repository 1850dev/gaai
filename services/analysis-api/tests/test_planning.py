from __future__ import annotations

from datetime import date

import pytest

from analytics_ai.analysis_state.reducer import AnalysisDelta, AnalysisStateReducer
from analytics_ai.authorization.service import AuthorizationService
from analytics_ai.cache.key import cache_key
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    AnalysisFocus,
    AnalysisState,
    DatePeriod,
    SemanticDefinition,
    SemanticScope,
)
from analytics_ai.orchestrator.planning import (
    ComparisonIntent,
    IntentPlan,
    PlanCompiler,
    RankingDirection,
    RankingIntent,
    SummaryStatistic,
    TimeGrain,
    VertexIntentPlanner,
    apply_reviewed_site_evaluation_plan,
    is_ambiguous_site_evaluation,
)
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.query_router.router import Route
from analytics_ai.repositories.memory import InMemoryRepository
from analytics_ai.semantic.resolver import SemanticResolutionError


def advanced_semantic_definitions() -> list[SemanticDefinition]:
    common = {
        "scope": SemanticScope.DATA_SOURCE_SET,
        "company_id": "company_a",
        "data_source_set_id": "company_a_web",
        "version": "a-web-v1",
    }
    return [
        SemanticDefinition(
            id="advanced-active-users",
            canonical_name="active_users",
            aliases=("UU", "アクティブユーザー"),
            kind="metric",
            ga4_mapping={"metric": "activeUsers"},
            **common,
        ),
        SemanticDefinition(
            id="advanced-sessions",
            canonical_name="sessions",
            aliases=("セッション",),
            kind="metric",
            aggregation="sum",
            ga4_mapping={"metric": "sessions"},
            **common,
        ),
        SemanticDefinition(
            id="advanced-date",
            canonical_name="date",
            aliases=("日付", "日別"),
            kind="date",
            ga4_mapping={"dimension": "date"},
            **common,
        ),
        SemanticDefinition(
            id="advanced-device",
            canonical_name="device_category",
            aliases=("デバイス",),
            kind="dimension",
            ga4_mapping={"dimension": "deviceCategory"},
            **common,
        ),
        SemanticDefinition(
            id="advanced-funnel",
            canonical_name="content_funnel",
            aliases=("コンテンツファネル",),
            kind="funnel",
            ga4_mapping={
                "funnel_steps": [
                    {"name": "記事閲覧", "event": "page_view"},
                    {"name": "予約完了", "event": "page_view_reservation_complete"},
                ]
            },
            **common,
        ),
    ]


def semantic_definitions() -> list[SemanticDefinition]:
    return [
        SemanticDefinition(
            id="web-cv",
            canonical_name="conversion",
            aliases=("CV", "コンバージョン"),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"event_name": "generate_lead"},
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="global-organic",
            canonical_name="organic_search",
            aliases=("Organic", "Organic Search"),
            kind="channel",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={
                "dimension": "sessionDefaultChannelGroup",
                "value": "Organic Search",
            },
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="global-mobile",
            canonical_name="mobile",
            aliases=("モバイル", "mobile"),
            kind="device",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={"dimension": "deviceCategory", "value": "mobile"},
            version="a-web-v1",
        ),
    ]


async def authorized_scope(repository: InMemoryRepository):
    return await AuthorizationService(repository).authorize_source_set(
        "user-a", "company_a_web"
    )


@pytest.mark.asyncio
async def test_compiler_preserves_conversation_filters_and_reuses_cache(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    definitions = semantic_definitions()
    compiler = PlanCompiler()
    reducer = AnalysisStateReducer()
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
    )

    initial = compiler.compile(
        IntentPlan(
            intent="current month conversions",
            period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 24)),
            metric_terms=("CV",),
        ),
        state=state,
        scope=scope,
        definitions=definitions,
    )
    assert initial.route is Route.GA4
    assert initial.arguments["metrics"] == ["keyEvents"]
    assert initial.arguments["dimension_filter"]["filter"]["field_name"] == (
        "eventName"
    )
    assert initial.descriptor is not None
    assert initial.descriptor.filters["source_scope"] == {
        "property_id": "222222222",
        "streams": [{"stream_id": "3333333333", "platform": "web"}],
    }
    state = reducer.apply(state, initial.delta).state
    state = reducer.apply(state, AnalysisDelta(dataset_ids=("dataset_001",))).state

    organic = compiler.compile(
        IntentPlan(intent="organic only", channel_term="Organic"),
        state=state,
        scope=scope,
        definitions=definitions,
    )
    expressions = organic.arguments["dimension_filter"]["and_group"]["expressions"]
    assert [item["filter"]["field_name"] for item in expressions] == [
        "eventName",
        "sessionDefaultChannelGroup",
    ]
    state = reducer.apply(state, organic.delta).state

    mobile = compiler.compile(
        IntentPlan(intent="mobile only", device_term="モバイル"),
        state=state,
        scope=scope,
        definitions=definitions,
    )
    expressions = mobile.arguments["dimension_filter"]["and_group"]["expressions"]
    assert [item["filter"]["string_filter"]["value"] for item in expressions] == [
        "generate_lead",
        "Organic Search",
        "mobile",
    ]
    state = reducer.apply(state, mobile.delta).state

    compared = compiler.compile(
        IntentPlan(
            intent="compare previous month",
            comparison=ComparisonIntent.PREVIOUS_MONTH,
        ),
        state=state,
        scope=scope,
        definitions=definitions,
    )
    assert compared.arguments["date_ranges"][1] == {
        "start_date": "2026-07-01",
        "end_date": "2026-07-24",
        "name": "comparison",
    }
    state = reducer.apply(state, compared.delta).state

    chart = compiler.compile(
        IntentPlan(intent="visualize", visualization_only=True),
        state=state,
        scope=scope,
        definitions=definitions,
    )
    assert chart.route is Route.CACHE
    assert chart.tool_name is None
    assert chart.delta.visualization_only is True


@pytest.mark.asyncio
async def test_compiler_builds_previous_year_date_range(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    compiled = PlanCompiler().compile(
        IntentPlan(
            intent="compare with previous year",
            period=DatePeriod(start=date(2024, 2, 1), end=date(2024, 2, 29)),
            comparison=ComparisonIntent.PREVIOUS_YEAR,
            metric_terms=("CV",),
        ),
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        scope=scope,
        definitions=semantic_definitions(),
    )

    assert compiled.arguments["date_ranges"][1] == {
        "start_date": "2023-02-01",
        "end_date": "2023-02-28",
        "name": "comparison",
    }


@pytest.mark.asyncio
async def test_monthly_grain_replaces_daily_state_and_uses_a_distinct_cache_key(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    definitions = [
        *semantic_definitions(),
        SemanticDefinition(
            id="web-active-users",
            canonical_name="active_users",
            aliases=("UU", "アクティブユーザー"),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"metric": "activeUsers"},
            version="a-web-v1",
        ),
    ]
    state = AnalysisState(
        data_source_set_id="company_a_web",
        period=DatePeriod(start=date(2026, 1, 1), end=date(2026, 8, 31)),
        focus=AnalysisFocus(metric="active_users", dimension="date"),
        semantic_version="a-web-v1",
    )
    compiler = PlanCompiler()

    daily = compiler.compile(
        IntentPlan(intent="daily active users", time_grain=TimeGrain.DAY),
        state=state,
        scope=scope,
        definitions=definitions,
    )
    monthly = compiler.compile(
        IntentPlan(intent="monthly active users", time_grain=TimeGrain.MONTH),
        state=state,
        scope=scope,
        definitions=definitions,
    )

    assert daily.arguments["dimensions"] == ["date"]
    assert monthly.arguments["dimensions"] == ["yearMonth"]
    assert daily.descriptor is not None
    assert monthly.descriptor is not None
    assert daily.descriptor.grain == "day"
    assert monthly.descriptor.grain == "month"
    assert monthly.descriptor.dimensions == ("year_month",)
    assert cache_key(daily.descriptor) != cache_key(monthly.descriptor)
    assert monthly.delta.dimension == "year_month"


@pytest.mark.asyncio
async def test_compiler_builds_bounded_metric_ranking(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    definitions = [
        *semantic_definitions(),
        SemanticDefinition(
            id="web-pv",
            canonical_name="page_views",
            aliases=("PV", "ページビュー"),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"metric": "screenPageViews"},
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="web-page",
            canonical_name="page_path",
            aliases=("ページ", "ページURL"),
            kind="dimension",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"dimension": "pagePathPlusQueryString"},
            version="a-web-v1",
        ),
    ]

    compiled = PlanCompiler().compile(
        IntentPlan(
            intent="page view ranking",
            period=DatePeriod(start=date(2026, 8, 24), end=date(2026, 8, 30)),
            metric_terms=("PV",),
            dimension_terms=("ページ",),
            ranking=RankingIntent(limit=10),
        ),
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        scope=scope,
        definitions=definitions,
    )

    assert compiled.arguments["limit"] == 10
    assert compiled.arguments["order_bys"] == [
        {"metric": {"metric_name": "screenPageViews"}, "desc": True}
    ]
    assert compiled.descriptor is not None
    assert compiled.descriptor.filters["ranking"] == {
        "metric": "page_views",
        "direction": "descending",
        "limit": 10,
    }


@pytest.mark.asyncio
async def test_compiler_requires_dimension_for_ranking(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    with pytest.raises(ValueError, match="ranking_dimension_is_required"):
        PlanCompiler().compile(
            IntentPlan(
                intent="conversion ranking",
                period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 24)),
                metric_terms=("CV",),
                ranking=RankingIntent(
                    direction=RankingDirection.ASCENDING,
                    limit=10,
                ),
            ),
            state=AnalysisState(
                data_source_set_id="company_a_web",
                semantic_version="a-web-v1",
            ),
            scope=scope,
            definitions=semantic_definitions(),
        )


@pytest.mark.asyncio
async def test_compiler_does_not_query_unavailable_web_conversion(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    definitions = [
        SemanticDefinition(
            id="web-conversion-unavailable",
            canonical_name="conversion",
            aliases=("CV", "コンバージョン"),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            version="a-web-v1",
            available=False,
        )
    ]

    with pytest.raises(
        SemanticResolutionError,
        match="semantic_term_unavailable_for_data_source_set",
    ):
        PlanCompiler().compile(
            IntentPlan(
                intent="current month conversions",
                period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 24)),
                metric_terms=("CV",),
            ),
            state=AnalysisState(
                data_source_set_id="company_a_web",
                semantic_version="a-web-v1",
            ),
            scope=scope,
            definitions=definitions,
        )


class RecordingGenerator:
    def __init__(self) -> None:
        self.prompt = ""

    async def generate(self, prompt: str) -> IntentPlan:
        self.prompt = prompt
        return IntentPlan(intent="conversions", metric_terms=("CV",))


class DailyGrainGenerator(RecordingGenerator):
    async def generate(self, prompt: str) -> IntentPlan:
        self.prompt = prompt
        return IntentPlan(
            intent="active users trend",
            metric_terms=("UU",),
            dimension_terms=("date",),
            time_grain=TimeGrain.DAY,
        )


class NoDimensionGenerator(RecordingGenerator):
    async def generate(self, prompt: str) -> IntentPlan:
        self.prompt = prompt
        return IntentPlan(intent="active users", metric_terms=("UU",))


@pytest.mark.asyncio
async def test_vertex_planner_receives_semantics_but_no_resource_or_mapping() -> None:
    generator = RecordingGenerator()
    planner = VertexIntentPlanner(
        Settings(environment="test"),
        generator=generator,
    )
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
    )

    result = await planner.plan(
        question="今月のCVはどう？",
        state=state,
        definitions=semantic_definitions(),
        today=date(2026, 8, 24),
    )

    assert result.metric_terms == ("CV",)
    assert "semantic_catalog" in generator.prompt
    assert "generate_lead" not in generator.prompt
    assert "222222222" not in generator.prompt


@pytest.mark.asyncio
async def test_vertex_planner_overrides_daily_with_explicit_monthly_grain() -> None:
    planner = VertexIntentPlanner(
        Settings(environment="test"),
        generator=DailyGrainGenerator(),
    )

    result = await planner.plan(
        question="月別でアクティブユーザー数を出して",
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        definitions=semantic_definitions(),
        today=date(2026, 9, 2),
    )

    assert result.time_grain is TimeGrain.MONTH


def test_vague_site_evaluation_requires_a_category_or_explicit_kpi() -> None:
    definitions = advanced_semantic_definitions()

    assert is_ambiguous_site_evaluation(
        "サイトを評価して",
        definitions,
        semantic_version="a-web-v1",
    )
    assert not is_ambiguous_site_evaluation(
        "セッションでサイトを評価して",
        definitions,
        semantic_version="a-web-v1",
    )
    assert not is_ambiguous_site_evaluation(
        "総合レポートを作って",
        definitions,
        semantic_version="a-web-v1",
    )


def test_reviewed_site_categories_override_model_selected_metrics() -> None:
    model_plan = IntentPlan(
        intent="model choice",
        skill_id=AnalysisSkillId.TREND,
        metric_terms=("invented_metric",),
        dimension_terms=("invented_dimension",),
        time_grain=TimeGrain.DAY,
    )

    acquisition = apply_reviewed_site_evaluation_plan("集客を確認", model_plan)
    engagement = apply_reviewed_site_evaluation_plan("エンゲージメント", model_plan)
    conversion = apply_reviewed_site_evaluation_plan("コンバージョン", model_plan)
    comprehensive = apply_reviewed_site_evaluation_plan("総合評価", model_plan)

    assert acquisition.skill_id is AnalysisSkillId.KPI_SNAPSHOT
    assert acquisition.metric_terms == ("active_users", "sessions")
    assert acquisition.dimension_terms == ()
    assert engagement.metric_terms == ("engagement_rate",)
    assert conversion.metric_terms == ("key_events",)
    assert comprehensive.skill_id is AnalysisSkillId.SITE_HEALTH_REPORT
    assert comprehensive.metric_terms == (
        "active_users",
        "sessions",
        "engagement_rate",
        "key_events",
    )
    assert comprehensive.dimension_terms == ()
    assert comprehensive.time_grain is None


def test_current_month_period_is_not_mistaken_for_monthly_grain() -> None:
    assert VertexIntentPlanner._time_grain_from_question("今月のUUは？") is None


def test_month_degree_is_recognized_as_monthly_grain() -> None:
    question = "過去半年間のPVの推移を月度で折れ線グラフで出力してください"

    assert VertexIntentPlanner._time_grain_from_question(question) is TimeGrain.MONTH


def test_table_request_is_detected_from_natural_language() -> None:
    assert VertexIntentPlanner._table_from_question("男女比を表形式で見せて")
    assert VertexIntentPlanner._table_from_question("年齢層を一覧で")
    assert not VertexIntentPlanner._table_from_question("男女比をグラフにして")


@pytest.mark.asyncio
async def test_vertex_planner_enforces_gender_dimension_when_requested() -> None:
    planner = VertexIntentPlanner(
        Settings(environment="test"),
        generator=NoDimensionGenerator(),
    )
    definitions = [
        *semantic_definitions(),
        SemanticDefinition(
            id="web-active-users",
            canonical_name="active_users",
            aliases=("UU",),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"metric": "activeUsers"},
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="web-user-gender",
            canonical_name="user_gender",
            aliases=("性別", "男女比"),
            kind="dimension",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"dimension": "userGender"},
            version="a-web-v1",
        ),
    ]

    result = await planner.plan(
        question="2026年8月の男女比をUUで見せて",
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        definitions=definitions,
        today=date(2026, 9, 2),
    )

    assert result.dimension_terms == ("user_gender",)


@pytest.mark.asyncio
async def test_vertex_planner_enforces_registered_channel_and_device_filters() -> None:
    planner = VertexIntentPlanner(
        Settings(environment="test"),
        generator=NoDimensionGenerator(),
    )

    result = await planner.plan(
        question="Organic Searchの中でモバイルだけ見せて",
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        definitions=semantic_definitions(),
        today=date(2026, 9, 2),
    )

    assert result.channel_term == "organic_search"
    assert result.device_term == "mobile"


class RecordingRankingGenerator(RecordingGenerator):
    async def generate(self, prompt: str) -> IntentPlan:
        self.prompt = prompt
        return IntentPlan(
            intent="page view ranking",
            metric_terms=("PV",),
            dimension_terms=("LP",),
        )


@pytest.mark.asyncio
async def test_vertex_planner_enforces_explicit_top_ten() -> None:
    planner = VertexIntentPlanner(
        Settings(environment="test"),
        generator=RecordingRankingGenerator(),
    )

    result = await planner.plan(
        question="今週のPVランキングを上位10で教えて",
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        definitions=semantic_definitions(),
        today=date(2026, 8, 30),
    )

    assert result.ranking == RankingIntent(limit=10)


@pytest.mark.asyncio
async def test_vertex_planner_uses_page_path_for_pv_ranking() -> None:
    planner = VertexIntentPlanner(
        Settings(environment="test"),
        generator=RecordingRankingGenerator(),
    )
    definitions = [
        *semantic_definitions(),
        SemanticDefinition(
            id="web-page",
            canonical_name="page_path",
            aliases=("ページ", "ページURL"),
            kind="dimension",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"dimension": "pagePathPlusQueryString"},
            version="a-web-v1",
        ),
    ]

    result = await planner.plan(
        question="今週のPVランキングを上位10で教えて",
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        definitions=definitions,
        today=date(2026, 8, 30),
    )

    assert result.dimension_terms == ("page_path",)


@pytest.mark.asyncio
async def test_multi_metric_trend_compiles_one_bounded_ga4_request(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    compiled = PlanCompiler().compile(
        IntentPlan(
            intent="daily users and sessions",
            skill_id=AnalysisSkillId.TREND,
            period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 31)),
            metric_terms=("UU", "セッション"),
            time_grain=TimeGrain.DAY,
        ),
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )

    assert compiled.tool_name == "run_report"
    assert compiled.arguments["metrics"] == ["activeUsers", "sessions"]
    assert compiled.arguments["dimensions"] == ["date"]
    assert compiled.arguments["limit"] == 1_500
    assert [metric.field for metric in compiled.metrics] == [
        "activeUsers",
        "sessions",
    ]
    assert compiled.descriptor is not None
    assert compiled.descriptor.skill_id == "ga4.trend.v1"


@pytest.mark.asyncio
async def test_explicit_total_rejects_non_additive_active_users(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)

    with pytest.raises(ValueError, match="non_additive_metric_cannot_be_summed"):
        PlanCompiler().compile(
            IntentPlan(
                intent="total users",
                period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 31)),
                metric_terms=("UU",),
                time_grain=TimeGrain.DAY,
                summary_statistic=SummaryStatistic.TOTAL,
            ),
            state=AnalysisState(
                data_source_set_id="company_a_web",
                semantic_version="a-web-v1",
            ),
            scope=scope,
            definitions=advanced_semantic_definitions(),
        )


@pytest.mark.asyncio
async def test_realtime_skill_has_no_core_date_range_and_is_not_cacheable(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    compiled = PlanCompiler().compile(
        IntentPlan(
            intent="realtime users by device",
            skill_id=AnalysisSkillId.REALTIME,
            metric_terms=("UU",),
            dimension_terms=("デバイス",),
        ),
        state=AnalysisState(
            data_source_set_id="company_a_web",
            period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 31)),
            semantic_version="a-web-v1",
        ),
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )

    assert compiled.tool_name == "run_realtime_report"
    assert "date_ranges" not in compiled.arguments
    assert compiled.arguments["dimensions"] == ["deviceCategory"]
    assert compiled.cacheable is False
    assert compiled.delta.clear_period is True
    assert compiled.delta.clear_comparison is True


@pytest.mark.asyncio
async def test_funnel_skill_accepts_only_registered_server_side_steps(
    repository: InMemoryRepository,
) -> None:
    scope = await authorized_scope(repository)
    compiled = PlanCompiler().compile(
        IntentPlan(
            intent="content funnel",
            skill_id=AnalysisSkillId.FUNNEL,
            funnel_term="コンテンツファネル",
            period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        ),
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
        scope=scope,
        definitions=advanced_semantic_definitions(),
    )

    assert compiled.tool_name == "run_funnel_report"
    assert compiled.arguments["funnel_steps"] == [
        {"name": "記事閲覧", "event": "page_view"},
        {"name": "予約完了", "event": "page_view_reservation_complete"},
    ]
    assert compiled.detail is True
    assert compiled.descriptor is not None
    assert compiled.descriptor.skill_id == "ga4.funnel.v1"
