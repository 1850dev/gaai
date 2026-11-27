"""Bounded, evidence-backed facts for planning and final analysis.

This module presents existing deterministic calculations; it never reads or sums
dataset rows. In particular, distinct users across dates/categories are not an
additive metric. Authorization and evidence checks belong to the caller.
"""

import re
from decimal import Decimal, InvalidOperation, localcontext
from itertools import zip_longest
from typing import Any

from analytics_ai.domain.models import AnalysisFinding, CalculationFact, CalculationKind

MAX_CALCULATIONS = 12
_COMPARISON_PERCENTAGE = re.compile(
    r"^（(前月比|前年比|比較期間比) ([+-]?\d+(?:\.\d+)?)%）"
)


def _number(value: str, *, percentage: bool = False) -> str:
    """Format a known numeric field, never arbitrary category/path text."""
    try:
        number = Decimal(value)
        if not number.is_finite():
            return value
        if percentage:
            with localcontext() as context:
                context.prec = max(28, len(number.as_tuple().digits) + 4)
                return format(number.quantize(Decimal("0.01")), ",.2f")
        rendered = format(number, ",f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except InvalidOperation:
        return value


def _formatted_text(finding: AnalysisFinding) -> str:
    text = finding.text
    # Only the generated terminal value slot may be formatted. A global numeric
    # replacement would corrupt dates, numbered categories and URL path segments.
    if finding.value is None:
        return text
    slot = f": {finding.value}"
    start = text.rfind(slot)
    if start < 0:
        return text
    suffix = text[start + len(slot) :]
    if suffix and not suffix.startswith(
        ("（前月比 ", "（前年比 ", "（比較期間比 ", "（ロバストスコア ")
    ):
        return text
    # Round the percentage already present in the finding. Do not reconstruct a
    # new comparison from other metadata or change its comparison basis.
    suffix = _COMPARISON_PERCENTAGE.sub(
        lambda match: f"（{match[1]} {_number(match[2], percentage=True)}%）", suffix
    )
    return f"{text[:start]}: {_number(finding.value)}{suffix}"


def _select_calculations(
    calculations: tuple[CalculationFact, ...],
) -> tuple[CalculationFact, ...]:
    if len(calculations) <= MAX_CALCULATIONS:
        return calculations
    # Summary facts (including period total/latest value appended by the
    # orchestrator) must not be lost behind a long ranking. Then balance ranking
    # and contribution facts instead of letting ten ranks hide all change drivers.
    summary = [fact for fact in calculations if fact.category is None]
    ranks = [
        fact
        for fact in calculations
        if fact.category is not None and fact.kind is CalculationKind.RANK
    ]
    other_categories = [
        fact
        for fact in calculations
        if fact.category is not None and fact.kind is not CalculationKind.RANK
    ]
    categories = [
        fact
        for pair in zip_longest(ranks, other_categories)
        for fact in pair
        if fact is not None
    ]
    return tuple((summary + categories)[:MAX_CALCULATIONS])


def finding_context(
    finding: AnalysisFinding, evidence_ids: list[str]
) -> dict[str, Any]:
    """Present one authorized finding without modifying numeric precision.

    ``text`` remains backward compatible, while ``formatted_text`` is suitable
    for fallback display. Truncation is explicit: these are selected calculated
    facts, not an exhaustive dataset or permission to infer missing totals.
    """
    selected = _select_calculations(finding.calculations)
    return {
        "id": finding.id,
        "text": finding.text,
        "formatted_text": _formatted_text(finding),
        "metric": finding.metric_field,
        "label": finding.metric_label,
        "value": finding.value,
        "comparison_value": finding.comparison_value,
        "change_rate": finding.change_rate,
        "comparison_kind": (
            finding.comparison_kind.value if finding.comparison_kind else None
        ),
        "dimension_field": finding.dimension_field,
        "dimension_kind": finding.dimension_kind,
        "dimension_fields": list(finding.dimension_fields),
        "category_values": [
            cell.model_dump(mode="json") for cell in finding.category_values
        ],
        "category_values_are_bounded_sample": True,
        "calculations": [fact.model_dump(mode="json") for fact in selected],
        "category_comparisons": [
            row.model_dump(mode="json") for row in finding.category_comparisons
        ],
        "calculation_count": len(finding.calculations),
        "calculations_truncated": len(selected) < len(finding.calculations),
        "evidence_ids": list(evidence_ids),
        "assumptions": finding.assumptions,
    }
