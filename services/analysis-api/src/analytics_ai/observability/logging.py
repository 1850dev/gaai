"""Minimal JSON logging and request-ID context."""

from __future__ import annotations

import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Any

request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id_context.get() or getattr(record, "request_id", ""),
        }
        for name in (
            "user_id",
            "company_id",
            "data_source_set_id",
            "session_id",
            "data_access_profile_id",
            "mcp_server",
            "mcp_tool",
            "query_hash",
            "query_id",
            "bytes_processed",
            "cache_status",
            "latency_ms",
            "error_code",
            "error_type",
            "provider_status",
            "model_phase",
            "model_attempt",
            "repair_attempt",
            "model_outcome",
            "validation_fields",
            "firebase_error_type",
            "evidence_ids",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
