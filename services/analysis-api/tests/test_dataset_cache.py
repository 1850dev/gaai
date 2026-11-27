from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import NotFound

from analytics_ai.cache.key import CacheDescriptor
from analytics_ai.cache.store import (
    DatasetCache,
    GcsCacheBlobs,
    InMemoryCacheBlobs,
    InMemoryCacheMetadata,
)


@pytest.mark.asyncio
async def test_gcs_cache_restores_raw_compressed_bytes(monkeypatch) -> None:
    blob = MagicMock()
    client = MagicMock()
    client.return_value.bucket.return_value.blob.return_value = blob
    monkeypatch.setattr("analytics_ai.cache.store.StorageClient", client)
    raw = b'[{"keyEvents":12}]'
    compressed = gzip.compress(raw)
    blob.download_as_bytes.side_effect = lambda raw_download=False: (
        compressed if raw_download else raw
    )
    metadata = InMemoryCacheMetadata()
    stored = await DatasetCache(metadata, InMemoryCacheBlobs()).put(
        descriptor(), [{"keyEvents": 12}]
    )
    cache = DatasetCache(metadata, GcsCacheBlobs("project", "test"))
    hit = await cache.get(descriptor())
    assert hit is not None
    assert hit.metadata.dataset_id == stored.metadata.dataset_id
    assert hit.rows == ({"keyEvents": 12},)
    blob.download_as_bytes.assert_called_once_with(raw_download=True)

    blob.download_as_bytes.side_effect = NotFound("object removed")
    assert await cache.get(descriptor()) is None


def descriptor(**updates: object) -> CacheDescriptor:
    values: dict[str, object] = {
        "company_id": "company_a",
        "data_source_set_id": "company_a_web",
        "source": "ga4",
        "date_range": ("2026-08-01", "2026-08-24"),
        "dimensions": ("date",),
        "metrics": ("keyEvents",),
        "filters": {"channel": "Organic Search"},
        "semantic_version": "a-web-v1",
    }
    values.update(updates)
    return CacheDescriptor.model_validate(values)


@pytest.mark.asyncio
async def test_dataset_cache_round_trip_is_canonical_and_scoped() -> None:
    metadata = InMemoryCacheMetadata()
    blobs = InMemoryCacheBlobs()
    cache = DatasetCache(metadata, blobs)
    original = descriptor(dimensions=("device", "date"))
    equivalent = descriptor(dimensions=("date", "device"))
    rows = [{"date": "2026-08-24", "device": "mobile", "keyEvents": 12}]

    stored = await cache.put(original, rows, evidence_id="evidence_001")
    hit = await cache.get(equivalent)

    assert hit is not None
    assert hit.metadata.dataset_id == stored.metadata.dataset_id
    assert hit.metadata.evidence_id == "evidence_001"
    assert hit.rows == tuple(rows)
    assert hit.metadata.blob_name.startswith("datasets/company_a/company_a_web/")
    assert await cache.get(descriptor(data_source_set_id="company_a_store")) is None


@pytest.mark.asyncio
async def test_expired_cache_entry_is_a_miss() -> None:
    current = datetime(2026, 8, 24, tzinfo=UTC)

    def now() -> datetime:
        return current

    cache = DatasetCache(
        InMemoryCacheMetadata(),
        InMemoryCacheBlobs(),
        ttl=timedelta(minutes=5),
        now=now,
    )
    item = descriptor()
    stored = await cache.put(item, [{"keyEvents": 1}])
    current += timedelta(minutes=6)
    assert await cache.get(item) is None
    history = await cache.get_by_key(
        stored.metadata.key,
        company_id="company_a",
        data_source_set_id="company_a_web",
        allow_expired=True,
    )
    assert history is not None
    assert history.rows == ({"keyEvents": 1},)


@pytest.mark.asyncio
async def test_cache_rejects_more_than_mcp_row_policy() -> None:
    cache = DatasetCache(InMemoryCacheMetadata(), InMemoryCacheBlobs())
    with pytest.raises(ValueError, match="cache_result_row_limit_exceeded"):
        await cache.put(descriptor(), [{"value": index} for index in range(3_001)])
