from typing import Any

import pytest

from analytics_ai.agent.facts import MAX_CALCULATIONS, finding_context
from analytics_ai.domain.models import (
    AnalysisFinding,
    CalculationFact,
    CalculationKind,
    ComparisonKind,
)


def finding(**changes: Any) -> AnalysisFinding:
    defaults: dict[str, Any] = {
        "id": "finding_" + "a" * 32,
        "session_id": "session",
        "user_id": "user",
        "company_id": "company",
        "data_source_set_id": "web",
        "dataset_id": "dataset",
        "query_hash": "b" * 64,
        "evidence_ids": ("evidence-1", "evidence-2"),
        "semantic_version": "v1",
        "text": "page_views: 14109012（前年比 7.960423574239160485034114746%）",
        "metric_field": "page_views",
        "metric_label": "ページビュー数",
        "value": "14109012",
        "comparison_value": "13068726",
        "change_rate": "0.07960423574239160485034114746",
        "comparison_kind": ComparisonKind.YOY,
    }
    return AnalysisFinding.model_validate(defaults | changes)


def test_context_formats_fallback_but_preserves_exact_numeric_metadata() -> None:
    item = finding()
    context = finding_context(item, ["evidence-2"])
    assert context["text"] == item.text
    assert context["formatted_text"] == "page_views: 14,109,012（前年比 7.96%）"
    assert context["metric"] == "page_views"
    assert context["label"] == "ページビュー数"
    assert context["value"] == "14109012"
    assert context["comparison_value"] == "13068726"
    assert context["change_rate"] == "0.07960423574239160485034114746"
    assert context["comparison_kind"] == "yoy"
    assert context["evidence_ids"] == ["evidence-2"]
    assert item.evidence_ids == ("evidence-1", "evidence-2")


@pytest.mark.parametrize(
    ("prefix", "suffix", "expected_suffix"),
    [
        ("20260630時点のアクティブユーザー数", "", ""),
        (
            "PVの上位は/products/14109012?year=2026",
            "（比較期間比 -12.3456%）",
            "（比較期間比 -12.35%）",
        ),
        (
            "PVの上位はカテゴリ（前年比 1234.567%）: 14109012/products",
            "（前年比 0.8517717357677922909602173486%）",
            "（前年比 0.85%）",
        ),
        (
            "期間合計のPV",
            "（前月比 1000%）（ロバストスコア 3.456: 異常候補）",
            "（前月比 1,000.00%）（ロバストスコア 3.456: 異常候補）",
        ),
    ],
)
def test_display_preserves_dates_categories_paths_and_calculation_basis(
    prefix: str, suffix: str, expected_suffix: str
) -> None:
    item = finding(text=f"{prefix}: 14109012{suffix}")
    context = finding_context(item, ["evidence-1"])
    assert context["formatted_text"] == f"{prefix}: 14,109,012{expected_suffix}"


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("対象期間のユーザーデータはありません。", None),
        ("/products: 14109012/detailsは未取得です。", "14109012"),
        ("前年比 7.960423574239%について追加調査が必要です。", "14109012"),
        ("PV: 14109012ではありません。", "14109012"),
    ],
)
def test_unrecognized_text_is_not_rewritten(text: str, value: str | None) -> None:
    assert (
        finding_context(finding(text=text, value=value), ["evidence-1"])[
            "formatted_text"
        ]
        == text
    )


def test_monthly_users_include_ranked_months_without_inventing_period_total() -> None:
    monthly = tuple(
        CalculationFact(
            kind=CalculationKind.RANK,
            value=str(100000 - month * 1000),
            category=f"2026{month:02}",
            rank=month,
        )
        for month in range(1, 7)
    )
    latest = CalculationFact(kind=CalculationKind.LATEST_VALUE, value="94000")
    item = finding(
        metric_field="active_users",
        metric_label="アクティブユーザー数",
        dimension_field="year_month",
        dimension_kind="date",
        text="202606時点のアクティブユーザー数: 94000",
        value="94000",
        comparison_value=None,
        change_rate=None,
        comparison_kind=None,
        calculations=(*monthly, latest),
        assumptions=("日付別結果の最新行を表示",),
    )
    context = finding_context(item, ["evidence-1"])
    assert context["dimension_field"] == "year_month"
    assert context["dimension_kind"] == "date"
    assert context["value"] == "94000"
    assert len(context["calculations"]) == 7
    assert context["calculations"] == [
        fact.model_dump(mode="json") for fact in item.calculations
    ]
    assert not any(fact["kind"] == "period_total" for fact in context["calculations"])
    assert context["assumptions"] == item.assumptions
    assert not context["calculations_truncated"]


def test_bounded_context_keeps_summary_rank_and_contribution_calculations() -> None:
    summary = (
        CalculationFact(kind=CalculationKind.VALUE, value="12345"),
        CalculationFact(
            kind=CalculationKind.DIFFERENCE,
            value="345",
            numerator="12345",
            denominator="12000",
            comparison_kind=ComparisonKind.YOY,
        ),
        CalculationFact(kind=CalculationKind.RATIO, value="1.02875", unit="ratio"),
        CalculationFact(kind=CalculationKind.CHANGE_RATE, value="0.02875"),
    )
    ranks = tuple(
        CalculationFact(
            kind=CalculationKind.RANK,
            value=str(1000 - i),
            category=f"channel-{i}",
            rank=i + 1,
        )
        for i in range(10)
    )
    contributions = tuple(
        CalculationFact(
            kind=CalculationKind.CONTRIBUTION,
            value="0.1234567890123456789012345678",
            category=f"channel-{i}",
            numerator="5",
            denominator="40.5",
            comparison_kind=ComparisonKind.YOY,
            unit="ratio",
        )
        for i in range(10)
    )
    latest = CalculationFact(kind=CalculationKind.LATEST_VALUE, value="678")
    item = finding(calculations=(*summary, *ranks, *contributions, latest))
    context = finding_context(item, ["evidence-1"])
    assert len(context["calculations"]) == MAX_CALCULATIONS
    assert context["calculation_count"] == 25
    assert context["calculations_truncated"]
    assert context["calculations"][:5] == [
        fact.model_dump(mode="json") for fact in (*summary, latest)
    ]
    assert context["calculations"][5] == ranks[0].model_dump(mode="json")
    assert context["calculations"][6] == contributions[0].model_dump(mode="json")
    assert len(item.calculations) == 25
