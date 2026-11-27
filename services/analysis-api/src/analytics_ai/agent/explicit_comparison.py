"""Recognize a bounded, fully specified categorical comparison table."""

import re

from analytics_ai.agent.workflows import (
    explicit_dimension_fields,
    explicit_semantic_fields,
)
from analytics_ai.domain.models import SemanticDefinition
from analytics_ai.orchestrator.planning import IntentPlan, explicit_month_comparison
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.semantic.resolver import normalize_term

_MONTH = r"(?:20\d{2}\s*年\s*)?(?:1[0-2]|0?[1-9])\s*月"
_PAIR = rf"{_MONTH}\s*(?:と|対|vs\.?|及び|および)\s*{_MONTH}"
_SEP = r"[、,。.!\s]*"
_ROLE = (
    rf"{_MONTH}\s*(?:を|が|は)\s*(?:対象(?:期間)?|比較基準|比較元|基準|ベース)"
    r"(?:にして|として|とし|に)?"
)
_COMPARE = r"比較(?:して(?:ください)?|してください|し)?"
_TABLE = (
    r"(?:表(?:形式)?|一覧)(?:で|に)"
    r"(?:出して|表示して|見せて|まとめて|して)(?:ください)?"
)


def explicit_comparison_table(
    question: str, definitions: list[SemanticDefinition]
) -> IntentPlan | None:
    """Use registered terms only. Decline extra or ambiguous instructions.

    In particular, additional filters, interpretation, ranking, and multi-query
    requests must still go through the normal planner.
    """
    if len(question) > 500:
        return None
    periods = explicit_month_comparison(question)
    if periods is None:
        return None
    fields = {
        "dimension": explicit_dimension_fields(question, definitions),
        "metric": explicit_semantic_fields(question, definitions, kind="metric"),
    }
    pattern: dict[str, str] = {}
    terms: dict[str, str] = {}
    for kind, names in fields.items():
        if len(names) != 1:
            return None
        match = [
            d
            for d in definitions
            if d.enabled
            and d.available
            and d.kind == kind
            and d.ga4_mapping
            and d.ga4_mapping.get(kind) == names[0]
        ]
        if len(match) != 1:
            return None
        definition = match[0]
        terms[kind] = definition.canonical_name
        aliases = sorted(
            {
                normalize_term(a)
                for a in (definition.canonical_name, *definition.aliases)
            }
            - {""},
            key=lambda a: (-len(a), a),
        )
        pattern[kind] = "(?:" + "|".join(map(re.escape, aliases)) + ")"

    axis = pattern["dimension"]
    metric = pattern["metric"]
    subject = (
        rf"(?:{axis}(?:別|ごと)(?:の|に){metric}を|"
        rf"{axis}を{_SEP}{metric}で|{metric}を{_SEP}{axis}(?:別|ごと)に)"
    )
    roles = rf"(?:{_ROLE}{_SEP}{_ROLE}{_SEP})?"
    output = rf"(?:{axis}(?:別|ごと)(?:の差を|に))?{_TABLE}"
    # Match the whole request so unhandled clauses cannot be silently dropped.
    grammar = (
        rf"{_PAIR}(?:の|について{_SEP}){subject}"
        rf"{_COMPARE}{_SEP}{roles}{output}{_SEP}"
    )
    if not re.fullmatch(grammar, normalize_term(question)):
        return None
    return IntentPlan(
        intent="指定期間の内訳を同じ項目で比較",
        skill_id=AnalysisSkillId.COMPARE,
        period=periods[0],
        comparison_period=periods[1],
        metric_terms=(terms["metric"],),
        dimension_terms=(terms["dimension"],),
        detail=True,
        preferred_chart_type="table",
    )
