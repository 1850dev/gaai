"""Small summary-only projection of already authorized findings. No I/O."""

from typing import Any


def observed_labels(facts: list[dict[str, Any]]) -> tuple[str, ...]:
    labels: set[str] = set()
    for fact in facts:
        values = [f.get("value") for f in fact.get("filters", [])]
        for cell in (
            *fact.get("category_values", []),
            *fact.get("category_comparisons", []),
        ):
            values.extend(cell.get("dimensions", {}).values())
        for value in values:
            if (
                isinstance(value, str)
                and 2 <= len(value) <= 300
                and any(c.isdecimal() for c in value)
                and (any(c.isalpha() for c in value) or value.startswith("/"))
            ):
                labels.add(value)
    return tuple(sorted(labels))


def summary_context(context: dict[str, Any]) -> dict[str, Any]:
    result = dict(context)
    # Findings already contain verified cells, filters and periods. Historical
    # sections and full staged rank plans repeated those values several times.
    result["historical_analyses"] = [
        {
            "session_id": hit.get("session_id"),
            "recorded_at": hit.get("recorded_at"),
            "historical": True,
            "finding_ids": [f["id"] for f in hit.get("facts", [])],
            "period": hit.get("section", {}).get("period"),
        }
        for hit in context.get("historical_analyses", [])
    ]
    staged = context.get("staged_analysis")
    if staged:
        result["staged_analysis"] = {
            key: staged[key]
            for key in (
                "objective",
                "phase",
                "request",
                "primary_field",
                "secondary_field",
                "metric_field",
                "rank_evidence_ids",
            )
            if key in staged
        } | {
            "items": [
                {
                    key: item[key]
                    for key in ("value", "status", "evidence_ids")
                    if key in item
                }
                for item in staged.get("items", [])
            ]
        }
    result["observed_labels"] = observed_labels(context.get("facts", []))
    return result
