"""A bounded review agenda over registered GA4 semantics, not raw API tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from analytics_ai.domain.models import AnalysisState, DatePeriod, SemanticDefinition
from analytics_ai.orchestrator.planning import (
    ComparisonIntent,
    IntentPlan,
    RankingIntent,
    TimeGrain,
)
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.semantic.resolver import normalize_term


def is_multifaceted_review(content: str) -> bool:
    return bool(re.search(r"多角|多面的|総合.*(?:分析|評価)|包括的|網羅的", content))


def explicit_half_year_review(
    content: str, definitions: list[SemanticDefinition], state: AnalysisState
) -> IntentPlan | None:
    """Reviewed entry path for explicit calendar-half reviews with known KPIs.

    Ambiguous fiscal years, filters, comparisons and special analyses still go
    through the conversational planner; do not silently discard those requests.
    """
    if not is_multifaceted_review(content) or re.search(
        r"年度|会計|だけ|絞|限定|除|以外|前年比|前月|比較|日別|週別|月別|月次|"
        r"SQL|JOIN|BigQuery|ファネル|異常|リアルタイム",
        content,
        re.IGNORECASE,
    ):
        return None
    match = re.search(r"(20\d{2})年(?:の)?(上期|上半期|下期|下半期)", content)
    if match is None:
        return None
    normalized = normalize_term(content)
    if any(
        d.kind in {"channel", "device", "filter"}
        and any(normalize_term(a) in normalized for a in d.aliases)
        for d in definitions
        if d.enabled and d.available
    ):
        return None
    metrics = tuple(
        dict.fromkeys(
            d.canonical_name
            for d in definitions
            if d.version == state.semantic_version
            and d.kind == "metric"
            and d.enabled
            and d.available
            and (
                any(
                    normalize_term(a) in normalized
                    for a in (d.canonical_name, *d.aliases)
                )
                or (
                    d.canonical_name == "page_views"
                    and re.search(r"page\s*views?", normalized)
                )
            )
        )
    )
    if not 1 <= len(metrics) <= 3:
        return None
    year = int(match[1])
    first_half = match[2].startswith("上")
    return IntentPlan(
        intent="暦年の半期を前年同期と比較して多角的に評価",
        period=DatePeriod(
            start=date(year, 1 if first_half else 7, 1),
            end=date(year, 6, 30) if first_half else date(year, 12, 31),
        ),
        metric_terms=metrics,
        comparison=ComparisonIntent.PREVIOUS_YEAR,
    )


@dataclass(frozen=True)
class ReviewAgenda:
    plans: tuple[tuple[str, IntentPlan], ...]

    @classmethod
    def create(
        cls,
        plan: IntentPlan,
        state: AnalysisState,
        definitions: list[SemanticDefinition],
    ) -> ReviewAgenda | None:
        period = plan.period or state.period
        metrics = plan.metric_terms or state.metrics
        if period is None or not metrics or len(metrics) > 3:
            return None
        if (
            plan.requires_raw_events
            or plan.requires_complex_join
            or plan.requires_text_ai
        ):
            return None
        # Authorization/semantic overrides are still checked by PlanCompiler on
        # every execution. No resource or credential is present in this agenda.
        base = plan.model_copy(
            update={
                "period": period,
                "metric_terms": metrics,
                "dimension_terms": (),
                "time_grain": None,
                "ranking": None,
                "visualization_only": False,
                "preferred_chart_type": None,
            }
        )
        plans = [
            (
                "期間全体",
                base.model_copy(
                    update={
                        "intent": "対象期間全体の主要指標を評価",
                        "skill_id": AnalysisSkillId.KPI_SNAPSHOT,
                    }
                ),
            ),
            (
                "時系列",
                base.model_copy(
                    update={
                        "intent": "期間内の推移と変動を確認",
                        "skill_id": AnalysisSkillId.TREND,
                        "time_grain": TimeGrain.MONTH
                        if (period.end - period.start).days > 62
                        else TimeGrain.DAY,
                    }
                ),
            ),
        ]
        available = {
            d.canonical_name
            for d in definitions
            if d.kind == "dimension"
            and d.enabled
            and d.available
            and d.version == state.semantic_version
        }
        for label, terms in (
            (
                "流入チャネル",
                (
                    "session_channel_group",
                    "channel",
                    "session_channel",
                    "session_default_channel_group",
                ),
            ),
            ("主要ページ", ("page_path", "landing_page")),
            ("デバイス", ("device_category",)),
        ):
            term = next((t for t in terms if t in available), None)
            if term is not None and len(plans) < 4:
                plans.append(
                    (
                        label,
                        base.model_copy(
                            update={
                                "intent": f"{label}別の主要指標を確認",
                                "skill_id": AnalysisSkillId.BREAKDOWN_RANK,
                                "dimension_terms": (term,),
                                "ranking": RankingIntent(limit=10),
                            }
                        ),
                    )
                )
        return cls(tuple(plans))

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(label for label, _ in self.plans)

    def next_plan(self, attempted: set[str]) -> tuple[str, IntentPlan] | None:
        return next((p for p in self.plans if p[0] not in attempted), None)


def query_signature(plan: IntentPlan, arguments: dict[str, object]) -> str:
    """Ignore intent wording; use the effective scoped and normalized request."""
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            {"arguments": arguments, "visualization_only": plan.visualization_only},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
