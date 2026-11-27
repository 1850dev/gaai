"""Normalize MCP responses into small scalar-only datasets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

type JsonScalar = str | int | float | bool | None


class ResultNormalizationError(ValueError):
    pass


def normalize_rows(
    payload: Mapping[str, Any],
    *,
    expected_dimensions: Sequence[str] = (),
    expected_metrics: Sequence[str] = (),
    max_rows: int = 3_000,
) -> list[dict[str, JsonScalar]]:
    report = _row_report(payload)
    rows = report.get("rows", [])
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ResultNormalizationError("mcp_rows_have_invalid_shape")
    if len(rows) > max_rows:
        raise ResultNormalizationError("mcp_result_row_limit_exceeded")

    if report is not payload:
        report = _funnel_header_block(report, rows)

    dimension_names = _header_names(
        report,
        ("dimension_headers", "dimensionHeaders"),
        expected_dimensions,
    )
    metric_names = _header_names(
        report,
        ("metric_headers", "metricHeaders"),
        expected_metrics,
    )
    all_names = (*dimension_names, *metric_names)
    if len(set(all_names)) != len(all_names):
        raise ResultNormalizationError("ga4_duplicate_headers")
    normalized: list[dict[str, JsonScalar]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ResultNormalizationError("mcp_row_has_invalid_shape")
        if _has_nested_values(row):
            normalized.append(_normalize_ga4_row(row, dimension_names, metric_names))
        else:
            normalized.append({str(key): _scalar(value) for key, value in row.items()})
    return normalized


def _funnel_header_block(
    report: Mapping[str, Any], rows: list[Any]
) -> Mapping[str, Any]:
    """Handle alpha's repeated metadata block, never merge duplicate values.

    Some live funnel responses repeat the complete metric header block multiple
    times but supply only one block of values per row. Accept only identical
    header objects and a unique, unambiguous value block on every returned row.
    All other duplicate shapes are rejected by the normal header checks.
    """
    key = "metric_headers" if "metric_headers" in report else "metricHeaders"
    headers = report.get(key)
    if not isinstance(headers, list) or not headers:
        return report
    if any(
        not isinstance(h, Mapping) or not isinstance(h.get("name"), str)
        for h in headers
    ):
        return report
    width = len({h["name"] for h in headers})
    if width == len(headers) or len(headers) % width:
        return report
    block = headers[:width]
    if len({h["name"] for h in block}) != width:
        return report
    if headers != block * (len(headers) // width):
        return report
    if any(
        not isinstance(row, Mapping)
        or len(_value_list(row, ("metric_values", "metricValues"))) != width
        for row in rows
    ):
        return report
    return {**report, key: block}


def _row_report(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select the tabular portion of a regular, realtime, or funnel response."""

    for key in ("funnel_table", "funnelTable"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return payload


def _header_names(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
    fallback: Sequence[str],
) -> tuple[str, ...]:
    headers: Any = None
    for key in keys:
        if key in payload:
            headers = payload[key]
            break
    if headers is None:
        return tuple(fallback)
    if not isinstance(headers, list):
        raise ResultNormalizationError("mcp_headers_have_invalid_shape")
    names: list[str] = []
    for header in headers:
        if not isinstance(header, Mapping) or not isinstance(header.get("name"), str):
            raise ResultNormalizationError("mcp_header_has_invalid_shape")
        names.append(header["name"])
    return tuple(names)


def _has_nested_values(row: Mapping[str, Any]) -> bool:
    return any(
        key in row
        for key in (
            "dimension_values",
            "dimensionValues",
            "metric_values",
            "metricValues",
        )
    )


def _normalize_ga4_row(
    row: Mapping[str, Any],
    dimension_names: tuple[str, ...],
    metric_names: tuple[str, ...],
) -> dict[str, JsonScalar]:
    dimension_values = _value_list(
        row,
        ("dimension_values", "dimensionValues"),
    )
    metric_values = _value_list(row, ("metric_values", "metricValues"))
    if len(dimension_values) != len(dimension_names):
        raise ResultNormalizationError("ga4_dimension_count_mismatch")
    if len(metric_values) != len(metric_names):
        raise ResultNormalizationError("ga4_metric_count_mismatch")
    return {
        **dict(zip(dimension_names, dimension_values, strict=True)),
        **dict(zip(metric_names, metric_values, strict=True)),
    }


def _value_list(
    row: Mapping[str, Any],
    keys: tuple[str, ...],
) -> list[JsonScalar]:
    values: Any = []
    for key in keys:
        if key in row:
            values = row[key]
            break
    if not isinstance(values, list):
        raise ResultNormalizationError("ga4_values_have_invalid_shape")
    return [_cell_value(value) for value in values]


def _cell_value(value: Any) -> JsonScalar:
    if isinstance(value, Mapping):
        if set(value) - {"value", "one_value"}:
            raise ResultNormalizationError("mcp_cell_has_invalid_shape")
        value = value.get("value", value.get("one_value"))
    return _scalar(value)


def _scalar(value: Any) -> JsonScalar:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ResultNormalizationError("mcp_result_contains_non_scalar_value")
