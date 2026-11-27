"""Tenant-scoped dataset cache with Firestore metadata and GCS result blobs."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any, Protocol

from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.storage import Client as StorageClient
from pydantic import BaseModel, ConfigDict, Field

from analytics_ai.cache.key import CacheDescriptor, cache_key, canonical_json

MAX_CACHE_BLOB_BYTES = 10 * 1024 * 1024
MAX_CACHE_ROWS = 3_000


class CacheBlobError(ValueError):
    """Persisted bytes cannot be restored as a valid cached dataset."""


class CacheMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_id: str
    company_id: str
    data_source_set_id: str
    descriptor_json: str
    blob_name: str
    row_count: int = Field(ge=0, le=MAX_CACHE_ROWS)
    evidence_id: str | None = None
    created_at: datetime
    expires_at: datetime


class CachedDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: CacheMetadata
    rows: tuple[dict[str, Any], ...]


class CacheMetadataBackend(Protocol):
    async def get(self, key: str) -> CacheMetadata | None: ...

    async def put(self, metadata: CacheMetadata) -> None: ...


class CacheBlobBackend(Protocol):
    async def read(self, name: str) -> bytes | None: ...

    async def write(self, name: str, payload: bytes) -> None: ...


class InMemoryCacheMetadata:
    def __init__(self) -> None:
        self.items: dict[str, CacheMetadata] = {}

    async def get(self, key: str) -> CacheMetadata | None:
        return self.items.get(key)

    async def put(self, metadata: CacheMetadata) -> None:
        self.items[metadata.key] = metadata


class InMemoryCacheBlobs:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    async def read(self, name: str) -> bytes | None:
        return self.items.get(name)

    async def write(self, name: str, payload: bytes) -> None:
        self.items[name] = payload


class FirestoreCacheMetadata:
    def __init__(self, project_id: str) -> None:
        self._client = firestore.AsyncClient(project=project_id)

    async def get(self, key: str) -> CacheMetadata | None:
        snapshot = await self._client.collection("datasetCache").document(key).get()
        return (
            CacheMetadata.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def put(self, metadata: CacheMetadata) -> None:
        await (
            self._client.collection("datasetCache")
            .document(metadata.key)
            .set(metadata.model_dump(mode="json"))
        )


class GcsCacheBlobs:
    def __init__(self, project_id: str, bucket_name: str) -> None:
        self._client = StorageClient(project=project_id)
        self._bucket = self._client.bucket(bucket_name)

    async def read(self, name: str) -> bytes | None:
        blob = self._bucket.blob(name)

        def download() -> bytes | None:
            try:
                # DatasetCache expects the stored gzip bytes, not GCS's
                # transparently decompressed media representation.
                return bytes(blob.download_as_bytes(raw_download=True))
            except NotFound:
                return None

        return await asyncio.to_thread(download)

    async def write(self, name: str, payload: bytes) -> None:
        blob = self._bucket.blob(name)
        blob.content_type = "application/json"
        blob.content_encoding = "gzip"
        await asyncio.to_thread(
            blob.upload_from_string,
            payload,
            content_type="application/json",
        )


class DatasetCache:
    """Cache whose key, metadata, and blob path all include tenant scope."""

    def __init__(
        self,
        metadata: CacheMetadataBackend,
        blobs: CacheBlobBackend,
        *,
        ttl: timedelta = timedelta(hours=6),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("cache TTL must be positive")
        self._metadata = metadata
        self._blobs = blobs
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(UTC))

    async def get(self, descriptor: CacheDescriptor) -> CachedDataset | None:
        key = cache_key(descriptor)
        metadata = await self._metadata.get(key)
        if metadata is None:
            return None
        if (
            metadata.company_id != descriptor.company_id
            or metadata.data_source_set_id != descriptor.data_source_set_id
            or metadata.descriptor_json != canonical_json(descriptor)
        ):
            return None
        return await self._read(metadata)

    async def get_by_key(
        self,
        key: str,
        *,
        company_id: str,
        data_source_set_id: str,
        allow_expired: bool = False,
    ) -> CachedDataset | None:
        metadata = await self._metadata.get(key)
        if metadata is None:
            return None
        descriptor_hash = hashlib.sha256(
            metadata.descriptor_json.encode("utf-8")
        ).hexdigest()
        if (
            metadata.company_id != company_id
            or metadata.data_source_set_id != data_source_set_id
            or descriptor_hash != key
        ):
            return None
        return await self._read(metadata, allow_expired=allow_expired)

    async def _read(
        self,
        metadata: CacheMetadata,
        *,
        allow_expired: bool = False,
    ) -> CachedDataset | None:
        if not allow_expired and metadata.expires_at <= self._now():
            return None
        compressed = await self._blobs.read(metadata.blob_name)
        if compressed is None:
            return None
        try:
            with gzip.GzipFile(fileobj=BytesIO(compressed)) as archived:
                raw = archived.read(MAX_CACHE_BLOB_BYTES + 1)
        except (gzip.BadGzipFile, EOFError, zlib.error) as error:
            raise CacheBlobError("cache_blob_is_not_valid_gzip") from error
        if len(raw) > MAX_CACHE_BLOB_BYTES:
            raise CacheBlobError("cache_blob_exceeds_policy")
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise CacheBlobError("cache_blob_is_not_valid_json") from error
        if not isinstance(decoded, list) or not all(
            isinstance(row, dict) for row in decoded
        ):
            raise CacheBlobError("cache_blob_has_invalid_shape")
        if len(decoded) != metadata.row_count or len(decoded) > MAX_CACHE_ROWS:
            raise CacheBlobError("cache_blob_row_count_mismatch")
        return CachedDataset(metadata=metadata, rows=tuple(decoded))

    async def put(
        self,
        descriptor: CacheDescriptor,
        rows: list[dict[str, Any]],
        *,
        evidence_id: str | None = None,
    ) -> CachedDataset:
        if len(rows) > MAX_CACHE_ROWS:
            raise ValueError("cache_result_row_limit_exceeded")
        raw = json.dumps(
            rows,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > MAX_CACHE_BLOB_BYTES:
            raise ValueError("cache_result_blob_limit_exceeded")
        key = cache_key(descriptor)
        dataset_id = f"dataset_{key[:24]}"
        blob_name = (
            f"datasets/{descriptor.company_id}/"
            f"{descriptor.data_source_set_id}/{key}.json.gz"
        )
        created = self._now()
        metadata = CacheMetadata(
            key=key,
            dataset_id=dataset_id,
            company_id=descriptor.company_id,
            data_source_set_id=descriptor.data_source_set_id,
            descriptor_json=canonical_json(descriptor),
            blob_name=blob_name,
            row_count=len(rows),
            evidence_id=evidence_id,
            created_at=created,
            expires_at=created + self._ttl,
        )
        await self._blobs.write(blob_name, gzip.compress(raw))
        await self._metadata.put(metadata)
        return CachedDataset(metadata=metadata, rows=tuple(rows))
