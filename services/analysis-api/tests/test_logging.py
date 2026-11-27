from __future__ import annotations

import json
import logging

from analytics_ai.observability.logging import JsonFormatter


def test_firebase_error_type_is_logged_without_sensitive_material() -> None:
    record = logging.LogRecord(
        name="analytics_ai.auth.firebase",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Firebase ID token rejected",
        args=(),
        exc_info=None,
    )
    record.error_code = "invalid_authentication"  # type: ignore[attr-defined]
    record.firebase_error_type = "InvalidIdTokenError"  # type: ignore[attr-defined]

    payload = json.loads(JsonFormatter().format(record))

    assert payload["error_code"] == "invalid_authentication"
    assert payload["firebase_error_type"] == "InvalidIdTokenError"
    assert "token" not in payload
