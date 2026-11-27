"""Numeric calculations that must never be delegated to an LLM."""

from __future__ import annotations

from decimal import Decimal, DivisionByZero


def difference(current: Decimal, previous: Decimal) -> Decimal:
    return current - previous


def ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    try:
        return numerator / denominator
    except DivisionByZero:
        return None


def change_rate(current: Decimal, previous: Decimal) -> Decimal | None:
    value = ratio(current - previous, previous)
    return value


def conversion_rate(conversions: Decimal, opportunities: Decimal) -> Decimal | None:
    """Return CVR as a 0-1 ratio; presentation decides percentage formatting."""

    return ratio(conversions, opportunities)


def contribution(part_change: Decimal, total_change: Decimal) -> Decimal | None:
    return ratio(part_change, total_change)


def rank_descending(
    values: dict[str, Decimal],
) -> list[tuple[str, Decimal, int]]:
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    return [(name, value, index + 1) for index, (name, value) in enumerate(ordered)]
