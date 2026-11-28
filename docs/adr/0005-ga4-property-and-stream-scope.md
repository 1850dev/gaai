# ADR 0005: GA4 Property and Stream scope

Status: Accepted

## Context

A GA4 Property can contain Web, iOS, and Android streams. Property-only authorization is too broad when one Data Source Set represents only the Web site. The official GA4 Data API exposes `platform` and numeric `streamId` dimensions and recommends using both when determining the stream type. Google tag/Measurement IDs such as `G-...` are not reporting resource identifiers.

The official `analytics-mcp` `run_report` and `run_realtime_report` tools accept a top-level `dimension_filter`. `run_funnel_report` instead accepts filters inside individual funnel steps and has no equivalent top-level filter.

## Decision

- `Ga4Source.property_id` remains the mandatory Property boundary.
- `Ga4Source.stream_scopes` optionally registers unique numeric Stream IDs paired with `web`, `iOS`, or `Android`.
- Empty `stream_scopes` explicitly means the whole registered Property is in scope. Mixed Web/App Properties must register their intended streams.
- After session and Data Source Set authorization is revalidated, the MCP Gateway overwrites `property_id` and injects the registered Stream scope into `run_report` and `run_realtime_report`.
- A proposed analytics filter is nested under `AND` with the fixed Stream scope. Callers cannot replace the fixed filter.
- Property ID and canonical Stream scope are included in the Dataset Cache descriptor.
- Stream-scoped `run_funnel_report` converts simple events to explicit filters and injects fixed scope into every funnel step. `segments` and `funnel_next_action` are fail-closed until their complete event scope can be enforced.
- Credential selection remains request-scoped per ADR 0004.

## Consequences

Web and App events cannot be mixed by changing an LLM-generated Property or filter. Reordering registered streams does not change the Cache key. Basic funnel steps are available; advanced Segment and Next Action analysis remains unavailable for stream-scoped sets until covered by a complete scope policy and acceptance tests.

## References

- [Google Analytics MCP guide](https://developers.google.com/analytics/devguides/MCP)
- [Google Analytics MCP repository](https://github.com/googleanalytics/google-analytics-mcp)
- [GA4 Data API dimensions](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema)
