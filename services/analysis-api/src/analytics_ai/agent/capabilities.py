"""Server-owned analysis coverage and prerequisites, not permission grants.

The agent chooses a relevant perspective, a measurement and a query skill. This
matrix describes implemented methods honestly; the compiler and compatibility
check still decide whether an individual request can execute.
"""

from typing import Any

from analytics_ai.domain.models import AnalysisSession, SemanticDefinition
from analytics_ai.semantic.resolver import SemanticResolutionError, SemanticResolver

# Fields express measurement semantics, not property IDs or freely executable SQL.
PERSPECTIVES = (
    (
        "acquisition",
        "流入・集客",
        (
            "sessionSource",
            "sessionMedium",
            "sessionSourceMedium",
            "sessionDefaultChannelGroup",
            "sessionCampaignName",
        ),
        "Session acquisition is not first-user acquisition or key-event attribution. "
        "A source alone does not identify its channel.",
    ),
    (
        "content",
        "ページ・ランディングページ",
        (
            "pagePath",
            "pagePathPlusQueryString",
            "pageTitle",
            "landingPage",
            "landingPagePlusQueryString",
            "hostName",
        ),
        "Page views concern all viewed pages; landing pages are session entry points. "
        "Do not substitute one for the other.",
    ),
    (
        "audience",
        "利用者・地域・属性",
        ("country", "region", "city", "userAgeBracket", "userGender", "language"),
        "Distinct users overlap across groups. Demographics may be thresholded; "
        "unknown values are not zero.",
    ),
    (
        "technology",
        "端末・ブラウザ・環境",
        ("deviceCategory", "browser", "operatingSystem", "platform"),
        "Separate grouping from filtering. Site context informs relevance, "
        "not permission to analyze or discuss device-specific improvements.",
    ),
    (
        "engagement",
        "利用状況・イベント",
        ("eventName", "pagePath", "sessionDefaultChannelGroup"),
        "Choose registered engagement measures and event counts. "
        "An event is not automatically a conversion.",
    ),
    (
        "time",
        "時系列・季節性・異常候補",
        ("date", "yearWeek", "yearMonth", "year"),
        "Period and grain are independent. Never sum distinct users or average "
        "aggregate rates as a site total. MAD requires at least seven observations "
        "and is not causal diagnosis.",
    ),
)


def effective_definitions(
    definitions: list[SemanticDefinition], session: AnalysisSession
) -> list[SemanticDefinition]:
    """Apply the same version/scope/availability resolver used by execution."""
    resolver = SemanticResolver(definitions)
    effective: dict[str, SemanticDefinition] = {}
    for term in sorted({d.canonical_name for d in definitions}):
        try:
            resolved = resolver.resolve(
                term,
                company_id=session.company_id,
                data_source_set_id=session.data_source_set_id,
                semantic_version=session.state.semantic_version,
            ).definition
        except SemanticResolutionError:
            continue
        # Resolve aliases too: an inherited alias must not bypass a lower-scope
        # unavailable or ambiguous definition in planning/completion checks.
        aliases: list[str] = []
        for alias in resolved.aliases:
            try:
                if (
                    resolver.resolve(
                        alias,
                        company_id=session.company_id,
                        data_source_set_id=session.data_source_set_id,
                        semantic_version=session.state.semantic_version,
                    ).definition.id
                    == resolved.id
                ):
                    aliases.append(alias)
            except SemanticResolutionError:
                continue
        effective[resolved.id] = resolved.model_copy(update={"aliases": tuple(aliases)})
    return list(effective.values())


def capability_context(definitions: list[SemanticDefinition]) -> dict[str, Any]:
    fields = {
        str(d.ga4_mapping["dimension"]): d.canonical_name
        for d in definitions
        if d.kind in {"dimension", "date"}
        and d.ga4_mapping
        and "dimension" in d.ga4_mapping
    }
    metrics = [d.canonical_name for d in definitions if d.kind == "metric"]
    funnels = [d.canonical_name for d in definitions if d.kind == "funnel"]
    return {
        "version": "1.0.0",
        "perspectives": [
            {
                "id": identifier,
                "label": label,
                "dimension_terms": [fields[field] for field in axes if field in fields],
                "status": "available_subject_to_compatibility"
                if any(field in fields for field in axes)
                else "missing_registered_definition",
                "correctness": rule,
            }
            for identifier, label, axes, rule in PERSPECTIVES
        ],
        "metric_terms": metrics,
        "methods": {
            "condition_change": {
                "contract": "analysis.condition_change.v1",
                "version": "1.0.0",
                "rule": (
                    "A server-provided single-edit contract separates changed_fields "
                    "from retained metrics, axes, periods, comparison and filters. "
                    "Apply expected exactly, including explicit no-comparison. "
                    "Only registered axis replacements and unambiguous baseline "
                    "changes are supported; this grants no new data access."
                ),
            },
            "filter_continuation": {
                "contract": "analysis.filter_continuation.v1",
                "version": "1.0.0",
                "rule": (
                    "A server-provided continuation_contract binds a short filter-only "
                    "followup to the existing metrics, axes, periods and time grain. "
                    "Only the requested registered filters may change. The compiler "
                    "and authorization still apply; changed plans are rejected before "
                    "execution. This is not a new data-access capability."
                ),
            },
            "grouped_breakdown": {
                "workflow": "analysis.grouped_breakdown.v1",
                "contract": (
                    "One metric, one period, two registered category axes. Rank "
                    "entities, then secondary top five within each (max ten entities)."
                ),
                "budget": (
                    "Execute the validated plan automatically: one rank query plus "
                    "one query per target (at most ten). Do not ask for a continuation "
                    "message. Existing time, authorization and result-size "
                    "guards apply."
                ),
                "input": (
                    "grouped_request primary/secondary terms and limit; "
                    "plan supplies metric and period."
                ),
                "limits": (
                    "Not global joint top combinations. No multi-period comparison "
                    "or arbitrary recursive DAG."
                ),
            },
            "cross_breakdown": {
                "skill": "ga4.cross_breakdown.v1",
                "axes": (
                    "2–4 registered dimensions in one run_report, "
                    "including date × category"
                ),
                "metrics": "1–4 compatible registered metrics; max 100 rows per period",
                "examples": [
                    "page × session source: page views",
                    "landing page × source / medium: sessions and active users",
                    "channel × device × page: page views",
                    "country × browser: users",
                    "event × device: event counts",
                    "month × channel: sessions; no summing distinct users",
                ],
                "evidence": (
                    "Exact joint cells, including single-period requests. "
                    "Separate marginal reports are insufficient."
                ),
                "limits": (
                    "Compatibility check mandatory. Not raw journeys, causal "
                    "attribution or server pivot. Top combinations are not top "
                    "pages then their sources."
                ),
            },
            "core": (
                "KPI, dimension breakdown/ranking, explicit-period comparison, "
                "trend, observed-value drilldown; use registered skills and terms."
            ),
            "realtime": (
                "Use ga4.realtime.v1 and its restricted realtime schema, "
                "not historical periods/comparisons."
            ),
            "funnel": {
                "skill": "ga4.funnel.v1",
                "registered_funnels": funnels,
                "prerequisite": (
                    "Reviewed ordered steps required. No ordinary metrics or "
                    "invented event sequence; ask when unregistered."
                ),
            },
            "anomaly": (
                "ga4.anomaly_scan.v1: bounded median/MAD screening, "
                "not a statistical causal claim."
            ),
            "commerce_or_conversion": (
                "Only registered and measured metrics/definitions. Never assume "
                "purchases or a CV goal for an information site. CVR requires "
                "a reviewed numerator/denominator contract."
            ),
        },
        "unavailable": [
            {
                "feature": "cohort_retention_and_server_pivot",
                "reason": (
                    "Not exposed by this pinned official MCP/tool compiler contract; "
                    "do not simulate cohort retention with newUsers."
                ),
            },
            {
                "feature": "raw_user_journey_or_crm_join",
                "reason": (
                    "Requires a different approved data path. "
                    "BigQuery is pending; do not execute it."
                ),
            },
            {
                "feature": "unregistered_custom_fields",
                "reason": (
                    "Admin metadata review/semantic registration required; "
                    "ordinary analysis cannot approve definitions."
                ),
            },
            {
                "feature": "causal_effect_or_target_attainment",
                "reason": (
                    "Observational GA4 data alone cannot prove causation "
                    "or attainment of an unspecified goal."
                ),
            },
        ],
        "completion": (
            "Cover the question's metrics, axes, periods and relevant perspectives "
            "with evidence. General KPI is not a substitute for a requested "
            "breakdown. Explicitly report missing coverage and next actions."
        ),
    }
