"""Canonical query JSON and tenant-scoped cache hash."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CacheDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    company_id: str
    data_source_set_id: str
    source: Literal["ga4", "bigquery", "bigquery_ai"]
    date_range: tuple[str, str]
    comparison_date_range: tuple[str, str] | None = None
    dimensions: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    filters: dict[str, Any] = Field(default_factory=dict)
    grain: str | None = None
    semantic_version: str
    skill_id: str | None = None
    skill_version: str | None = None
    compiler_version: str | None = None
    tool_name: str | None = None


def canonical_json(descriptor: CacheDescriptor) -> str:
    payload = descriptor.model_dump(mode="json")
    payload["dimensions"] = sorted(set(payload["dimensions"]))
    payload["metrics"] = sorted(set(payload["metrics"]))
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def cache_key(descriptor: CacheDescriptor) -> str:
    return hashlib.sha256(canonical_json(descriptor).encode("utf-8")).hexdigest()
