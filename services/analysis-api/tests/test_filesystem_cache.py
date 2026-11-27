from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from analytics_ai.cache.filesystem import FilesystemCacheBlobs
from analytics_ai.cache.store import DatasetCache, InMemoryCacheMetadata
from analytics_ai.config import Settings
from tests.test_dataset_cache import descriptor


@pytest.mark.asyncio
async def test_filesystem_cache_shared_across_instances_and_tenant_scoped(
    tmp_path: Path,
) -> None:
    metadata = InMemoryCacheMetadata()
    writer = DatasetCache(metadata, FilesystemCacheBlobs(str(tmp_path)))
    stored = await writer.put(descriptor(), [{"keyEvents": 5}])
    reader = DatasetCache(metadata, FilesystemCacheBlobs(str(tmp_path)))
    found = await reader.get(descriptor())
    assert found is not None
    assert found.rows == ({"keyEvents": 5},)
    assert (
        await reader.get_by_key(
            stored.metadata.key,
            company_id="company_b",
            data_source_set_id="company_a_web",
        )
        is None
    )
    assert await asyncio.to_thread(
        lambda: all(p.stat().st_mode & 0o077 == 0 for p in tmp_path.iterdir())
    )


@pytest.mark.asyncio
async def test_filesystem_atomic_writes_and_no_path_traversal(tmp_path: Path) -> None:
    blobs = FilesystemCacheBlobs(str(tmp_path))
    name = "../../outside"
    await asyncio.gather(
        blobs.write(name, b"first" * 1000), blobs.write(name, b"second" * 1000)
    )
    assert blobs.path(name).parent == tmp_path
    assert await blobs.read(name) in {b"first" * 1000, b"second" * 1000}
    assert await asyncio.to_thread(lambda: len(list(tmp_path.iterdir()))) == 1
    assert await blobs.read("missing") is None


@pytest.mark.parametrize("environment", ["dev", "stg", "prod"])
def test_filesystem_cache_forbidden_outside_local(environment: str) -> None:
    with pytest.raises(ValueError, match="filesystem cache requires local"):
        Settings(
            environment=environment,
            cache_backend="filesystem",
            repository_backend="firestore",
        )


def test_filesystem_requires_durable_metadata() -> None:
    with pytest.raises(ValueError, match="filesystem cache requires local"):
        Settings(
            environment="local", cache_backend="filesystem", repository_backend="memory"
        )
    assert (
        Settings(
            environment="local",
            cache_backend="filesystem",
            repository_backend="firestore",
        ).resolved_cache_backend
        == "filesystem"
    )
