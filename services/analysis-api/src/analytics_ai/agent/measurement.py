"""Evidence-specific measurement context. No new inference, query or permission."""

from typing import Any


def measurement_context(context: dict[str, Any]) -> dict[str, Any]:
    """Describe what each cited finding contains, without merging unrelated axes."""
    evidence = []
    for fact in context.get("facts", []):
        fields = set(fact.get("dimension_fields", ()))
        fields.update(
            f.get("field_name", f.get("field", "")) for f in fact.get("filters", [])
        )
        evidence.append(
            {
                "fact_id": fact["id"],
                "evidence_ids": fact.get("evidence_ids", []),
                "metric": fact.get("metric"),
                "label": fact.get("label"),
                "dimension_and_filter_fields": sorted(fields),
                "source_without_medium_or_channel": bool(
                    (
                        "sessionSource" in fields
                        and not fields.intersection(
                            {
                                "sessionMedium",
                                "sessionSourceMedium",
                                "sessionDefaultChannelGroup",
                            }
                        )
                    )
                    or (
                        "firstUserSource" in fields
                        and not fields.intersection(
                            {
                                "firstUserMedium",
                                "firstUserSourceMedium",
                                "firstUserDefaultChannelGroup",
                            }
                        )
                    )
                ),
                "bounded_sample": fact.get("category_values_are_bounded_sample", True),
            }
        )
    return {
        "id": "analysis.measurement_reasoning.v1",
        "evidence": evidence,
        "interpretation": (
            "Read fields PER finding, not their union. Source labels alone do not "
            "establish channel, user identity, membership or visitor purpose. "
            "Page/source aggregates are not navigation sequences or click events. "
            "A registered metric is not proof it has been measured in this query."
        ),
        "proposals": (
            "Link purpose to hypothesis and validation. "
            "Metrics can be partial proxies; "
            "unknown measurement is a prerequisite, not a reason to halt the answer. "
            "Do not imply that extra GA4 requests are already approved or sufficient."
        ),
    }


MEASUREMENT_INSTRUCTIONS = """
Use measurement_context to distinguish observed evidence, site context and
untested hypotheses. A goal in Settings is not a measured outcome. Cite facts
for observations and label the improvement mechanism as a hypothesis.
Use exact source names in titles, hypotheses and narrative: google does not mean
organic search; dm_passport_app does not prove a native-app user or a member.
Only medium/channel fields or filters IN THE SAME cited finding can support
that classification. Never borrow another report's channel to classify a cell.
Page/source PV does not show a click, an ordered path or "visits via this page".
Propose checking current links and instrumentation before a navigation experiment.
Do not describe existing links as missing without inspected evidence.
Registered metrics may be candidates for a future query, not already observed.
engagedSessions is not a count of effective operations or link clicks: duration,
key events or multiple views can qualify a session. engagementRate is the share
of engaged sessions, not satisfaction or task completion. Use them as proxies.
Suggest a registered source/medium or engagement breakdown when useful, but do
not offer arbitrary next-page/path analysis. For future click measurement FIRST
verify instrumentation, semantic definitions and supported tools. A before/after
PV or engagement comparison cannot establish which link was clicked.
For click/sequence/offline outcome validation, state prerequisites and measurement
verification first. Raw journeys are unavailable through the current tool path;
only a reviewed registered funnel can evaluate its own defined sequence.
This agent cannot implement tracking or change the site. Ask the responsible
person to verify existing event definitions; never offer to add tracking yourself.
These rules apply to TITLES and the premise of a hypothesis too. Appending
"not confirmed" elsewhere, or saying "app etc.", does not repair an inferred
app audience. Say "参照元名 dm_passport_app と記録された閲覧", not
"アプリ等外部連携流入向け". Say "google と記録された閲覧", not "検索流入".
Do not compare unmeasured transitions first and check tracking afterwards:
verify definitions/instrumentation/tool support BEFORE offering such evaluation.
When data_acquisition_allowed is false, use existing facts only; do not turn a
measurement prerequisite into a request to fetch now. State what remains unknown.
For every new proposal, fill evaluation: goal_alignment, metric_reason,
metric_limitations, measurement_status and prerequisites. Explain how it serves
the actual site goal, why the metric is useful, what it cannot determine, and
what to verify before measuring. Use measurement_status=proxy for a useful
indirect measure, or needs_confirmation when instrumentation/definition must be
checked. Missing goals can be stated as unknown; do not block ordinary analysis.
success_metric may be a registered canonical metric or null. Use null when no
suitable metric is established; do not invent events or force PV as success.
A proxy is not target attainment, observed causation or a guarantee of uplift.
If a user's goal is finding information efficiently, higher PV or dwell time can
also mean difficulty: explain this ambiguity and consider a task-based usability
check or confirmed click instrumentation, without inventing outcome measurements.
When proposals are supplied for the summary, preserve their goal alignment and
measurement caveats; do not silently replace them with a generic PV-growth goal.
"""
