from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from analytics_ai.analysis_state.reducer import AnalysisDelta, AnalysisStateReducer
from analytics_ai.cache.key import CacheDescriptor, cache_key
from analytics_ai.domain.models import (
    AnalysisState,
    DatePeriod,
    Ga4Platform,
    Ga4Source,
    Ga4StreamScope,
    SemanticDefinition,
    SemanticScope,
)
from analytics_ai.evidence.calculations import (
    change_rate,
    contribution,
    conversion_rate,
    difference,
    rank_descending,
    ratio,
)
from analytics_ai.query_router.router import QueryRouter, Route, RoutingRequest
from analytics_ai.semantic.resolver import SemanticResolutionError, SemanticResolver
from analytics_ai.visualization.planner import ChartType, VisualizationPlanner


def test_semantic_resolves_uu_and_set_specific_cv() -> None:
    definitions = [
        SemanticDefinition(
            id="global-users",
            canonical_name="active_users",
            aliases=("UU", "ユーザー数"),
            kind="metric",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={"metric": "activeUsers"},
            version="a-web-v1",
        ),
        SemanticDefinition(
            id="web-cv",
            canonical_name="conversion",
            aliases=("CV",),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"event_name": "generate_lead"},
            version="a-web-v1",
        ),
    ]
    resolver = SemanticResolver(definitions)
    users = resolver.resolve(
        "ＵＵ",
        company_id="company_a",
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
    )
    conversion = resolver.resolve(
        "cv",
        company_id="company_a",
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
    )
    assert users.definition.ga4_mapping == {"metric": "activeUsers"}
    assert conversion.definition.ga4_mapping == {"event_name": "generate_lead"}


def test_web_source_set_explicitly_blocks_unrelated_company_conversion() -> None:
    definitions = [
        SemanticDefinition(
            id="company-conversion",
            canonical_name="conversion",
            aliases=("CV", "コンバージョン"),
            kind="metric",
            scope=SemanticScope.COMPANY,
            company_id="company_a",
            ga4_mapping={"event_name": "in_app_purchase"},
            version="a-web-v1",
        ),
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
        ),
    ]

    with pytest.raises(
        SemanticResolutionError,
        match="semantic_term_unavailable_for_data_source_set",
    ):
        SemanticResolver(definitions).resolve(
            "CV",
            company_id="company_a",
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        )


def test_unavailable_semantic_cannot_carry_a_data_mapping() -> None:
    with pytest.raises(
        ValueError,
        match="unavailable definitions cannot carry data mappings",
    ):
        SemanticDefinition(
            id="invalid-web-conversion",
            canonical_name="conversion",
            aliases=("CV",),
            kind="metric",
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"event_name": "in_app_purchase"},
            version="a-web-v1",
            available=False,
        )


def test_ga4_stream_scope_requires_numeric_unique_stream_ids() -> None:
    with pytest.raises(ValueError, match="String should match pattern"):
        Ga4StreamScope(stream_id="G-ABC123", platform=Ga4Platform.WEB)

    with pytest.raises(ValueError, match="GA4 stream IDs must be unique"):
        Ga4Source(
            property_id="222222222",
            display_name="A社 Web",
            stream_scopes=(
                Ga4StreamScope(stream_id="3333333333", platform=Ga4Platform.WEB),
                Ga4StreamScope(stream_id="3333333333", platform=Ga4Platform.IOS),
            ),
        )


def test_conversation_continues_and_visualization_reuses_dataset() -> None:
    reducer = AnalysisStateReducer()
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
    )
    state = reducer.apply(
        state,
        AnalysisDelta(
            period=DatePeriod(
                start=date(2026, 8, 1),
                end=date(2026, 8, 24),
            ),
            metric="conversion",
            dataset_ids=("dataset_001",),
            route="ga4",
        ),
    ).state
    state = reducer.apply(state, AnalysisDelta(channel="Organic Search")).state
    state = reducer.apply(state, AnalysisDelta(device="mobile")).state
    state = reducer.apply(state, AnalysisDelta(compare_previous_month=True)).state
    final = reducer.apply(state, AnalysisDelta(visualization_only=True))
    assert final.state.focus.metric == "conversion"
    assert final.state.focus.channel == "Organic Search"
    assert final.state.focus.device == "mobile"
    assert final.state.comparison_period == DatePeriod(
        start=date(2026, 7, 1),
        end=date(2026, 7, 24),
    )
    assert final.state.dataset_ids == ("dataset_001",)
    assert final.visualization_only is True
    assert (
        QueryRouter().choose(
            RoutingRequest(intent="visualize", visualization_only=True)
        )
        is Route.CACHE
    )


def test_cache_key_is_canonical_and_tenant_scoped() -> None:
    common = {
        "company_id": "company_a",
        "data_source_set_id": "company_a_web",
        "source": "ga4",
        "date_range": ("2026-08-01", "2026-08-24"),
        "filters": {"channel": "Organic Search"},
        "semantic_version": "a-web-v1",
    }
    first = CacheDescriptor(
        **common,
        dimensions=("date", "device"),
        metrics=("sessions", "activeUsers"),
    )
    second = CacheDescriptor(
        **common,
        dimensions=("device", "date"),
        metrics=("activeUsers", "sessions"),
    )
    other = first.model_copy(update={"data_source_set_id": "company_a_store"})
    assert cache_key(first) == cache_key(second)
    assert cache_key(first) != cache_key(other)


def test_numeric_and_visualization_rules_are_deterministic() -> None:
    assert difference(Decimal("120"), Decimal("100")) == Decimal("20")
    assert ratio(Decimal("120"), Decimal("100")) == Decimal("1.2")
    assert change_rate(Decimal("72"), Decimal("100")) == Decimal("-0.28")
    assert conversion_rate(Decimal("12"), Decimal("300")) == Decimal("0.04")
    assert contribution(Decimal("10"), Decimal("25")) == Decimal("0.4")
    assert ratio(Decimal("1"), Decimal("0")) is None
    assert rank_descending({"Organic": Decimal("20"), "Paid": Decimal("30")}) == [
        ("Paid", Decimal("30"), 1),
        ("Organic", Decimal("20"), 2),
    ]
    spec = VisualizationPlanner().plan(
        title="CV推移",
        dataset_id="dataset_001",
        evidence_ids=("evidence_001",),
        metric_field="keyEvents",
        metric_label="CV",
        dimension_field="date",
        dimension_kind="date",
    )
    assert spec.type is ChartType.LINE
    assert spec.x == "date"

    table = VisualizationPlanner().plan(
        title="男女別UU",
        dataset_id="dataset_001",
        evidence_ids=("evidence_001",),
        metric_field="activeUsers",
        metric_label="UU",
        dimension_field="userGender",
        dimension_kind="dimension",
        detail=True,
    )
    assert table.type is ChartType.TABLE
    assert table.category == "userGender"


def test_visualization_title_uses_resolved_period_dimension_and_metric() -> None:
    title = VisualizationPlanner.analysis_title(
        period=DatePeriod(
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        ),
        metric_field="activeUsers",
        metric_label="active_users",
        dimension_field="userGender",
        dimension_kind="dimension",
    )

    assert title == "2026年6月 性別ごとのアクティブユーザー数"


def test_previous_year_comparison_handles_leap_day() -> None:
    reducer = AnalysisStateReducer()
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
        period=DatePeriod(
            start=date(2024, 2, 1),
            end=date(2024, 2, 29),
        ),
    )

    compared = reducer.apply(state, AnalysisDelta(compare_previous_year=True)).state

    assert compared.comparison_period == DatePeriod(
        start=date(2023, 2, 1),
        end=date(2023, 2, 28),
    )


def test_only_one_automatic_comparison_period_is_allowed() -> None:
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
        period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 24)),
    )

    with pytest.raises(ValueError, match="only one comparison"):
        AnalysisStateReducer().apply(
            state,
            AnalysisDelta(
                compare_previous_month=True,
                compare_previous_year=True,
            ),
        )


def test_text_ai_requires_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="bigquery_ai_not_enabled"):
        QueryRouter().choose(
            RoutingRequest(intent="classify text", requires_text_ai=True)
        )
