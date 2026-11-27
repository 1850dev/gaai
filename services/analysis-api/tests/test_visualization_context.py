from datetime import date

import pytest

from analytics_ai.domain.models import AnalysisFilter, DatePeriod
from analytics_ai.visualization.planner import VisualizationPlanner
from tests.test_agent_history_reuse import saved_group as saved_group_fixture


@pytest.mark.parametrize(
    "field,value,label",
    [
        ("pagePathPlusQueryString", "/sapporo/", "ページ"),
        ("landingPagePlusQueryString", "/sale/?campaign=test", "ランディングページ"),
        ("deviceCategory", "mobile", "デバイス"),
        ("sessionDefaultChannelGroup", "Organic Search", "チャネル"),
        ("eventName", "click", "イベント名"),
        ("country", "Japan", "国"),
    ],
)
def test_titles_include_exact_resolved_scope(field, value, label):
    title = VisualizationPlanner.analysis_title(
        period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
        metric_field="screenPageViews",
        metric_label="page_views",
        dimension_field="sessionSource",
        dimension_kind="dimension",
        filters=(
            AnalysisFilter(
                semantic_name="scope", kind="filter", field_name=field, value=value
            ),
        ),
    )
    assert title == f"2026年5月 {label}「{value}」｜参照元ごとのページビュー数"


def test_multiple_axes_and_filters_are_not_reduced_to_one_dimension():
    filters = tuple(
        AnalysisFilter(semantic_name=f"scope_{i}", kind="filter", field_name=f, value=v)
        for i, (f, v) in enumerate(
            (("pagePath", "/detail/"), ("deviceCategory", "mobile"))
        )
    )
    title = VisualizationPlanner.analysis_title(
        period=None,
        metric_field="screenPageViews",
        metric_label="ページビュー数・アクティブユーザー数",
        metric_labels=("page_views", "active_users"),
        dimension_field="sessionSource",
        dimension_kind="dimension",
        dimensions=("sessionSource", "sessionMedium"),
        filters=filters,
    )
    assert "参照元 × メディアごとのページビュー数・アクティブユーザー数" in title
    assert "ページ「/detail/」" in title and "デバイス「mobile」" in title


@pytest.mark.parametrize(
    "direction,label", [("ascending", "下位"), ("descending", "上位")]
)
def test_comparison_and_ranking_are_explicit(direction, label):
    subtitle = VisualizationPlanner.analysis_subtitle(
        comparison_period=DatePeriod(start=date(2026, 4, 1), end=date(2026, 4, 30)),
        ranking_limit=5,
        ranking_direction=direction,
        ranking_metric="page_views",
    )
    assert "比較元：2026年4月" in subtitle
    assert f"ページビュー数{label}5件" in subtitle


@pytest.mark.asyncio
async def test_child_titles_and_legacy_restore_use_original_query_not_current_state(
    repository,
):
    (
        repo,
        _agent,
        orchestrator,
        calls,
        source,
        _target,
        _cited,
    ) = await saved_group_fixture.__wrapped__(repository)
    messages = await repo.list_messages(source.id)
    plots = [m for m in messages if m.visualization]
    children = plots[1:]
    assert len(children) == 5
    assert len({m.visualization["title"] for m in children}) == 5
    for i, message in enumerate(children):
        assert f"ページ「/page-{i}/」" in message.visualization["title"]
        assert "上位5件" in message.visualization["subtitle"]
    # Old stored titles have no filter context; enrich read-only from each cache.
    legacy = [
        m.model_copy(
            update={
                "visualization": {
                    **m.visualization,
                    "title": "旧タイトル",
                    "subtitle": None,
                }
            }
        )
        for m in plots
    ]
    changed = source.model_copy(
        update={
            "state": source.state.model_copy(
                update={
                    "period": DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 31)),
                    "filters": (
                        AnalysisFilter(
                            semantic_name="wrong",
                            kind="filter",
                            field_name="pagePath",
                            value="/unrelated/",
                        ),
                    ),
                }
            )
        }
    )
    restored = await orchestrator.restore_results(session=changed, messages=legacy)
    assert len(restored) == 6
    for i, result in enumerate(restored[1:]):
        assert f"ページ「/page-{i}/」" in result.visualization.title
        assert result.visualization.title.startswith("2026年5月")
        assert "/unrelated/" not in result.visualization.title
    assert all(m.visualization["title"] == "旧タイトル" for m in legacy)
    assert len(calls) == 6  # no new GA4 request to repair old titles
