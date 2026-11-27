"""Immutable session binding to data access scope, not a source's display name."""

import hashlib
import json

from analytics_ai.domain.models import DataSourceSet


def source_scope_fingerprint(source: DataSourceSet) -> str:
    payload = source.model_dump(mode="json")
    scope = {
        key: payload[key]
        for key in ("id", "company_id", "data_access_profile_id", "ga4", "bigquery")
    }
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def published_source_fingerprint(source: DataSourceSet) -> str:
    """Matches the immutable full-source baseline used by legacy registration."""
    return hashlib.sha256(
        json.dumps(
            source.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
