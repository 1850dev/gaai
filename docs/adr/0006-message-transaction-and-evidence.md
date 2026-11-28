# ADR 0006: Message transaction and Evidence pipeline

Status: Accepted for the basic streaming vertical slice

## Context

Chat history is not Analysis State, and a Message request may race with another request for the same Session. MCP responses also have server-specific shapes and must not flow directly into Findings, the outer LLM, or the browser.

## Decision

- The endpoint reauthorizes the Session before planning. MCP Gateway independently reauthorizes again before every external invocation.
- Planner output never contains resource or credential authority. Trusted compilation and Gateway injection remain the only source of executable resource IDs.
- MCP rows are normalized to scalar-only dictionaries and capped at 3,000. Unexpected nested/raw shapes fail closed.
- Canonical Query Scope selects a tenant-scoped Dataset Cache entry. Cache metadata points to immutable Dataset Evidence.
- Reusable Dataset Evidence contains no originating User, Session, or Profile identifier; those correlations remain in access-controlled Audit records.
- Numeric Finding values and comparison rates are calculated with `Decimal` in code. Non-additive dimension results are not blindly summed.
- Before publishing, authorization is checked again. Session revision, Assistant Message, and optional Finding are committed in one optimistic Firestore transaction.
- User Message and failure Audit remain traceable even if analysis fails. Raw question text and result rows are not written to Audit.
- Visualization-only requests load the existing scoped Cache entry by its hash and do not call MCP.

## Consequences

The API supports both a typed JSON response and POST SSE with `status`, `finding`, `visualization`, `evidence`, `done`, and `error` events. Fine-grained planner/MCP progress callbacks, bounded retry/cancellation, request idempotency, and Vertex-based prose composition from small calculated facts remain Production Blockers. Cache blobs and Evidence may remain as valid orphaned analysis artifacts if a Session transaction loses a race; they stay tenant-scoped and expire under Cache policy.
