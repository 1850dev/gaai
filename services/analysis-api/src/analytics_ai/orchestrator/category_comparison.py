"""Bounded, exact-key comparison facts shared by every categorical GA4 analysis."""

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from itertools import zip_longest

from analytics_ai.domain.models import CategoryComparison, CategoryValue
from analytics_ai.evidence.calculations import change_rate, difference
from analytics_ai.orchestrator.normalization import JsonScalar


def category_values(
    rows: Sequence[Mapping[str, JsonScalar]], *, fields: tuple[str, ...], metric: str
) -> tuple[CategoryValue, ...]:
    """Persist a bounded exact-key sample in query order, including without comparison.

    Duplicate keys are ambiguous, not additive. Incomplete tuples, invalid values
    and overly large labels are omitted rather than truncating identity strings.
    """
    if not fields:
        return ()
    values: dict[tuple[str, ...], Decimal | None] = {}
    for row in rows:
        if any(row.get(field) is None for field in fields):
            continue
        key = tuple(str(row[field]) for field in fields)
        if any(len(label) > 500 for label in key):
            continue
        try:
            value = Decimal(str(row.get(metric)))
        except InvalidOperation:
            values[key] = None
            continue
        if not value.is_finite():
            values[key] = None
            continue
        values[key] = None if key in values else value
    return tuple(
        CategoryValue(dimensions=dict(zip(fields, key, strict=True)), value=str(value))
        for key, value in values.items()
        if value is not None
    )[:12]


def compare_categories(
    current: Sequence[Mapping[str, JsonScalar]],
    baseline: Sequence[Mapping[str, JsonScalar]],
    *,
    fields: tuple[str, ...],
    metric: str,
) -> tuple[CategoryComparison, ...]:
    """Never aggregate duplicate keys or infer a missing category's traffic.

    Selection covers high volume, large absolute changes and baseline-only rows
    within the retrieved subset. It is not a full-property change ranking.
    """
    if not fields:
        return ()

    def index(
        rows: Sequence[Mapping[str, JsonScalar]],
    ) -> dict[tuple[str, ...], Decimal | None]:
        result: dict[tuple[str, ...], Decimal | None] = {}
        for row in rows:
            if any(row.get(field) is None for field in fields):
                continue
            key = tuple(str(row[field]) for field in fields)
            value: Decimal | None
            try:
                value = Decimal(str(row.get(metric)))
                if not value.is_finite():
                    value = None
            except InvalidOperation:
                value = None
            result[key] = None if key in result else value
        return result

    now, before = index(current), index(baseline)
    volume = sorted(now, key=lambda key: (-(now[key] or Decimal(0)), key))
    changes = sorted(
        (
            key
            for key in now.keys() & before.keys()
            if now[key] is not None and before[key] is not None
        ),
        key=lambda key: (
            -abs((now[key] or Decimal(0)) - (before[key] or Decimal(0))),
            key,
        ),
    )
    missing = sorted(before.keys() - now.keys())
    # Interleave perspectives so a shorter answer also keeps change/missing cases.
    selected = list(
        dict.fromkeys(
            key
            for group in zip_longest(volume[:10], changes[:5], missing[:5])
            for key in group
            if key is not None
        )
    )
    results: list[CategoryComparison] = []
    for key in selected:
        current_value, baseline_value = now.get(key), before.get(key)
        delta = rate = None
        if current_value is not None and baseline_value is not None:
            delta = difference(current_value, baseline_value)
            rate = change_rate(current_value, baseline_value)
        results.append(
            CategoryComparison(
                dimensions=dict(zip(fields, key, strict=True)),
                current_value=str(current_value) if current_value is not None else None,
                comparison_value=str(baseline_value)
                if baseline_value is not None
                else None,
                difference=str(delta) if delta is not None else None,
                change_rate=str(rate) if rate is not None else None,
                status="ambiguous"
                if (key in now and current_value is None)
                or (key in before and baseline_value is None)
                else "comparison_only"
                if key not in now
                else "current_only"
                if key not in before
                else "matched",
            )
        )
    return tuple(results)
