from __future__ import annotations

from copy import deepcopy

import pytest

from analytics_ai.orchestrator.normalization import (
    ResultNormalizationError,
    normalize_rows,
)


def test_normalizes_official_ga4_camel_case_response() -> None:
    rows = normalize_rows(
        {
            "dimensionHeaders": [{"name": "dateRange"}],
            "metricHeaders": [{"name": "activeUsers"}],
            "rows": [
                {
                    "dimensionValues": [{"value": "current"}],
                    "metricValues": [{"value": "12"}],
                }
            ],
        }
    )
    assert rows == [{"dateRange": "current", "activeUsers": "12"}]


def test_rejects_nested_raw_result_values() -> None:
    with pytest.raises(
        ResultNormalizationError,
        match="mcp_result_contains_non_scalar_value",
    ):
        normalize_rows({"rows": [{"raw": {"secret": "value"}}]})


def test_rejects_more_than_result_policy() -> None:
    with pytest.raises(ResultNormalizationError, match="mcp_result_row_limit_exceeded"):
        normalize_rows({"rows": [{"value": index} for index in range(3_001)]})


def test_normalizes_funnel_table_instead_of_visualization_payload() -> None:
    rows = normalize_rows(
        {
            "funnelTable": {
                "dimensionHeaders": [{"name": "funnelStepName"}],
                "metricHeaders": [{"name": "activeUsers"}],
                "rows": [
                    {
                        "dimensionValues": [{"value": "商品詳細"}],
                        "metricValues": [{"value": "120"}],
                    }
                ],
            },
            "funnelVisualization": {"rows": [{"not": "tabular"}]},
        }
    )

    assert rows == [{"funnelStepName": "商品詳細", "activeUsers": "120"}]


@pytest.mark.parametrize("camel", [False, True])
@pytest.mark.parametrize("repeats", [2, 3])
def test_funnel_repeated_header_block_with_one_value_block(
    camel: bool, repeats: int
) -> None:
    # Observed alpha response shape; synthetic counts, not customer data.
    metrics = [
        {"name": "activeUsers", "type_": "TYPE_INTEGER"},
        {"name": "funnelStepCompletionRate", "type_": "TYPE_DOUBLE"},
    ]
    table = {
        "dimensionHeaders" if camel else "dimension_headers": [
            {"name": "funnelStepName"}
        ],
        "metricHeaders" if camel else "metric_headers": metrics * repeats,
        "rows": [
            {
                "dimensionValues": [{"value": "1. View"}],
                "metricValues": [{"value": "100"}, {"value": "0.5"}],
            },
            {
                "dimensionValues": [{"value": "2. Engage"}],
                "metricValues": [{"value": "50"}, {"value": "0"}],
            },
        ],
    }
    payload = {"funnelTable" if camel else "funnel_table": table}
    original = deepcopy(payload)
    assert normalize_rows(payload) == [
        {
            "funnelStepName": "1. View",
            "activeUsers": "100",
            "funnelStepCompletionRate": "0.5",
        },
        {
            "funnelStepName": "2. Engage",
            "activeUsers": "50",
            "funnelStepCompletionRate": "0",
        },
    ]
    assert payload == original


@pytest.mark.parametrize("camel", [False, True])
def test_empty_funnel_with_repeated_headers_is_empty_not_fabricated(
    camel: bool,
) -> None:
    payload = {
        "funnelTable" if camel else "funnel_table": {
            "dimensionHeaders": [{"name": "funnelStepName"}],
            "metricHeaders": [{"name": "activeUsers"}] * 2,
            "rows": [],
        }
    }
    original = deepcopy(payload)
    assert normalize_rows(payload) == []
    assert payload == original


@pytest.mark.parametrize("funnel", [False, True])
def test_duplicate_metric_values_cannot_silently_overwrite(funnel: bool) -> None:
    report = {
        "metricHeaders": [{"name": "activeUsers"}] * 2,
        "rows": [{"metricValues": [{"value": "100"}, {"value": "50"}]}],
    }
    with pytest.raises(ResultNormalizationError, match="duplicate"):
        normalize_rows({"funnelTable": report} if funnel else report)


def test_funnel_nonidentical_or_partial_header_repetition_is_rejected() -> None:
    for headers in (
        [{"name": "a"}, {"name": "b"}, {"name": "a"}],
        [{"name": "a", "type_": "TYPE_INTEGER"}, {"name": "a", "type_": "TYPE_DOUBLE"}],
    ):
        with pytest.raises(ResultNormalizationError):
            normalize_rows(
                {
                    "funnelTable": {
                        "metricHeaders": headers,
                        "rows": [{"metricValues": [{"value": "1"}]}],
                    }
                }
            )
