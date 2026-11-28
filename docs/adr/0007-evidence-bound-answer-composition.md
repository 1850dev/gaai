# ADR 0007 — Evidence-bound answer composition

Status: accepted

Vertex AI does not calculate or rewrite analytics numbers. The application sends only the untrusted user question, normalized Analysis State period/focus, and small code-calculated Finding facts. It does not send Dataset rows, Property/Dataset/Credential scope, or MCP arguments.

Vertex returns a structured `AnswerDraft`: a non-numeric headline, an ordered subset of supplied Finding IDs, and an optional non-numeric next question. Pydantic rejects digits, numeric Unicode characters, percentage/currency markers, duplicate IDs, and extra fields. Trusted code rejects unknown Finding IDs and inserts each persisted Finding text verbatim.

This limits the model to presentation and follow-up selection while Evidence-backed code remains the numeric source of truth. Composition failure never discards a valid analysis; the Orchestrator falls back to deterministic Finding text and writes an error audit containing only model name, Finding IDs, Evidence IDs, and safe error type.
