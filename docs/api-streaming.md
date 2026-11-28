# Analysis streaming API

`POST /api/sessions/{session_id}/messages/stream` accepts the same authenticated JSON body as the non-streaming Message endpoint:

```json
{"content":"今月のUUを前月と比較して"}
```

The Frontend sends a unique `Idempotency-Key` header for each submitted message. The accepted form is 8-128 ASCII letters, digits, `.`, `_`, `:`, or `-`. The raw key is not persisted; a session-scoped SHA-256-derived User Message ID is created atomically. Reuse of a key cannot execute MCP twice and returns `analysis_request_duplicate`. Completed-response replay is not implemented yet; the client restores committed history with `GET /api/sessions/{session_id}/messages`.

The response uses `text/event-stream`. Because the request requires a Firebase Bearer token and a JSON body, the Frontend uses `fetch()` with a stream reader rather than browser `EventSource`.

Events are typed as follows:

- `status`: request accepted and displayed as live progress. Fine-grained planner/cache/MCP stages are not implemented yet.
- `finding`: one deterministic Finding linked to Evidence.
- `visualization`: validated Visualization Specification, never chart code.
- `evidence`: scoped Dataset Evidence metadata; no originating User/Session/Profile identifier.
- `done`: complete typed Analysis Response, including normalized rows for rendering.
- `error`: safe error code after streaming headers have already been sent.

Authentication, Session authorization, Feature Gate checks, and the initial Data Source Set boundary check complete before the stream starts. The Orchestrator and MCP Gateway reauthorize again during execution and before transactional publication. Responses use `no-cache, no-store` and disable reverse-proxy buffering.

The Frontend applies intermediate Finding, Evidence, and Visualization events immediately, then treats `done` as the authoritative snapshot. It renders KPI, line, bar, donut, or bounded table output from the validated specification and normalized rows. The UI displays `Cache hit`/`Cache miss`; the corresponding scoped Query Hash and Evidence ID remain in application Audit.

The complete analysis has a 200-second deadline and each external MCP execution has a 180-second deadline by default. Disconnect cancellation is propagated to the Orchestrator and recorded as `analysis_cancelled`. GA4 read-only transient API errors use at most three exponential-backoff attempts with jitter. Permanent GA4 failures and BigQuery execution failures are not retried. In particular, a BigQuery response that is ambiguous after live execution must be reconciled by Query ID/Audit/Cache rather than blindly submitted again.
