"""Versioned contracts for unambiguous, single-condition followups.

This is a workflow policy, not a new GA4 capability or permission. Other kinds of
requests still use the ordinary planner; never guess that a new task is narrowing.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from analytics_ai.agent.conversation import explicit_filter_requirements, query_context
from analytics_ai.analysis_state.reducer import AnalysisStateReducer
from analytics_ai.domain.models import AnalysisState, DatePeriod, SemanticDefinition
from analytics_ai.orchestrator.planning import CompiledPlan
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.semantic.resolver import normalize_term

CORE_SKILLS = {
    None,
    AnalysisSkillId.KPI_SNAPSHOT.value,
    AnalysisSkillId.SITE_HEALTH_REPORT.value,
    AnalysisSkillId.TREND.value,
    AnalysisSkillId.BREAKDOWN_RANK.value,
    AnalysisSkillId.CROSS_BREAKDOWN.value,
    AnalysisSkillId.COMPARE.value,
    AnalysisSkillId.DRILLDOWN.value,
}


@dataclass(frozen=True)
class ContinuationContract:
    expected: AnalysisState
    anchor: AnalysisState
    changed_fields: tuple[str, ...] = ("filters",)
    identifier: str = "analysis.filter_continuation.v1"
    version: str = "1.0.0"

    def context(self) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "version": self.version,
            "changed_fields": self.changed_fields,
            "rule": (
                "Apply exactly the expected conditions. Only changed_fields are "
                "user-requested changes; preserve all other conditions."
            ),
            "expected": query_context(self.expected),
        }

    def requirements(self, definitions: list[SemanticDefinition]) -> dict[str, Any]:
        def fields(terms: tuple[str, ...], kind: str) -> tuple[str, ...]:
            mapping = {
                d.canonical_name: str(d.ga4_mapping[kind])
                for d in definitions
                if d.ga4_mapping
                and kind in d.ga4_mapping
                and d.kind in ({kind, "date"} if kind == "dimension" else {kind})
            }
            # Missing registrations stay unmet; never replace them with new terms.
            return tuple(mapping.get(term, term) for term in terms)

        axes = fields(self.expected.dimensions, "dimension")
        return {
            "required_metric_fields": fields(self.expected.metrics, "metric"),
            "required_dimension_fields": axes,
            "required_dimension_groups": (axes,) if len(axes) > 1 else (),
            "required_period": self.expected.period,
            "required_comparison_period": self.expected.comparison_period,
            "comparison_required_exactly": True,
            "required_filters": self.expected.filters,
        }

    def validate(
        self, compiled: CompiledPlan, *, state: AnalysisState | None = None
    ) -> None:
        # Applying to expected would hide an omitted change (e.g. a missing new
        # comparison baseline). Validate what will actually execute from anchor.
        candidate = (
            AnalysisStateReducer().apply(state or self.anchor, compiled.delta).state
        )
        changed = [
            name
            for name in (
                "period",
                "comparison_period",
                "metrics",
                "dimensions",
                "time_grain",
            )
            if getattr(candidate, name) != getattr(self.expected, name)
        ]
        if set(candidate.filters) != set(self.expected.filters):
            changed.append("filters")
        if compiled.tool_name != "run_report" or (
            compiled.skill is None or compiled.skill.id.value not in CORE_SKILLS
        ):
            changed.append("capability")
        if changed:
            raise ValueError("followup_context_changed: " + ", ".join(changed))


def filter_continuation(
    question: str, state: AnalysisState, definitions: list[SemanticDefinition]
) -> ContinuationContract | None:
    """Recognize only a complete, short positive narrowing command.

    Full matching intentionally excludes dates, changed axes/metrics, negation,
    advice, no-fetch requests and ambiguous alternatives. Unsupported wording is
    not silently rewritten. Definitions must already be scope/version resolved.
    """
    metrics = state.metrics or ((state.focus.metric,) if state.focus.metric else ())
    dimensions = state.dimensions or (
        (state.focus.dimension,) if state.focus.dimension else ()
    )
    if state.period is None or not metrics or state.selected_skill not in CORE_SKILLS:
        return None
    aliases = {
        re.sub(r"\s+", "", normalize_term(alias))
        for d in definitions
        if d.enabled
        and d.available
        and d.kind in {"channel", "device", "filter"}
        and d.version == state.semantic_version
        for alias in (d.canonical_name, *d.aliases)
        if alias
    }
    if not aliases:
        return None
    term = (
        "(?:"
        + "|".join(re.escape(a) for a in sorted(aliases, key=lambda a: (-len(a), a)))
        + ")"
    )
    normalized = re.sub(r"\s+", "", normalize_term(question)).strip("。.!！")
    pattern = (
        r"(?:その中で|この中で|同じ条件で|今の条件で|その条件で|では|じゃあ)?"
        + term
        + r"(?:(?:と|かつ|、)"
        + term
        + r")*"
        r"(?:だけ|のみ)(?:に)?"
        r"(?:絞って|限定して|して|見せて|見て|分析して|表示して)?"
        r"(?:ください|下さい|ほしい)?"
    )
    if re.fullmatch(pattern, normalized) is None:
        return None
    filters = explicit_filter_requirements(question, state, definitions)
    if filters is None:
        return None
    expected = state.model_copy(
        update={
            "metrics": metrics,
            "dimensions": dimensions,
            "filters": filters,
            "focus": state.focus.model_copy(
                update={
                    "channel": next(
                        (f.value for f in filters if f.kind == "channel"), None
                    ),
                    "device": next(
                        (f.value for f in filters if f.kind == "device"), None
                    ),
                }
            ),
        }
    )
    return ContinuationContract(expected, state)


def condition_change_continuation(
    question: str, state: AnalysisState, definitions: list[SemanticDefinition]
) -> ContinuationContract | None:
    """Recognize a whole single-edit command, never keyword-match a new task.

    Effective scoped definitions must be supplied by the caller. Compound,
    negative, uncertain, unknown and conflicting edits use the ordinary planner.
    No property/resource identifiers or values are inferred here.
    """
    metrics = state.metrics or ((state.focus.metric,) if state.focus.metric else ())
    dimensions = state.dimensions or (
        (state.focus.dimension,) if state.focus.dimension else ()
    )
    if state.period is None or not metrics or state.selected_skill not in CORE_SKILLS:
        return None
    normalized = re.sub(r"\s+", "", normalize_term(question)).strip("。.!！")
    normalized = re.sub(
        r"^(?:同じ条件で|今の条件で|その条件で|では|じゃあ)", "", normalized
    )
    ending = r"(?:変更して|変えて|切り替えて|切替えて|して)(?:ください|ほしい)?"
    update: dict[str, Any] = {}
    baseline = re.fullmatch(
        r"(?:比較基準|比較期間|比較対象|比較元)(?:だけ|のみ|を)?"
        r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?:に|へ)" + ending,
        normalized,
    )
    if baseline:
        year, month = int(baseline["year"]), int(baseline["month"])
        try:
            update["comparison_period"] = DatePeriod(
                start=date(year, month, 1),
                end=date(year, month, calendar.monthrange(year, month)[1]),
            )
        except ValueError:
            return None
    elif re.fullmatch(
        r"(?:期間比較|比較)(?:だけ|のみ|を)?(?:解除して|なしにして|やめて)"
        r"(?:ください|ほしい)?",
        normalized,
    ):
        update["comparison_period"] = None
    else:
        available = [
            d
            for d in definitions
            if d.enabled
            and d.available
            and d.version == state.semantic_version
            and d.kind in {"dimension", "date"}
            and d.ga4_mapping
            and "dimension" in d.ga4_mapping
        ]
        aliases: dict[str, set[str]] = {}
        for definition in available:
            for alias in (definition.canonical_name, *definition.aliases):
                key = re.sub(r"\s+", "", normalize_term(alias))
                if key:
                    aliases.setdefault(key, set()).add(definition.canonical_name)
        swap = re.fullmatch(
            r"(?P<old>.+?)(?:から|を)(?P<new>.+?)(?:に|へ)" + ending,
            normalized,
        )
        if not swap:
            return None
        old_terms, new_terms = (
            aliases.get(swap["old"], set()),
            aliases.get(swap["new"], set()),
        )
        if len(old_terms) != 1 or len(new_terms) != 1:
            return None
        old, new = next(iter(old_terms)), next(iter(new_terms))
        if old not in dimensions or (new in dimensions and new != old):
            return None
        by_name = {d.canonical_name: d for d in available}
        if by_name[old].kind != by_name[new].kind:
            return None
        if by_name[new].kind == "date":
            grains = {
                "date": "day",
                "yearWeek": "week",
                "yearMonth": "month",
                "year": "year",
            }
            mapping = by_name[new].ga4_mapping or {}
            grain = grains.get(str(mapping.get("dimension")))
            if grain is None:
                return None
            # Compiler puts the time axis first; all non-date axes are retained.
            update["dimensions"] = (new, *(d for d in dimensions if d != old))
            update["time_grain"] = grain
        else:
            update["dimensions"] = tuple(new if d == old else d for d in dimensions)
    expected = state.model_copy(
        update={"metrics": metrics, "dimensions": dimensions, **update}
    )
    expected = expected.model_copy(
        update={
            "focus": expected.focus.model_copy(
                update={
                    "metric": metrics[0],
                    "dimension": expected.dimensions[0]
                    if expected.dimensions
                    else None,
                }
            ),
        }
    )
    return ContinuationContract(
        expected, state, tuple(update), "analysis.condition_change.v1"
    )
