"""Local-only shared Docker volume; production uses GCS blobs."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from analytics_ai.cache.store import MAX_CACHE_BLOB_BYTES


class FilesystemCacheBlobs:
    def __init__(self, directory: str) -> None:
        root = Path(directory)
        if not root.is_absolute() or root == Path("/"):
            raise ValueError("cache_directory_must_be_specific_absolute_path")
        self.root = root

    def path(self, name: str) -> Path:
        # Resource labels are never filesystem path components (no traversal).
        return self.root / (hashlib.sha256(name.encode()).hexdigest() + ".gz")

    async def read(self, name: str) -> bytes | None:
        def load() -> bytes | None:
            try:
                fd = os.open(self.path(name), os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as source:
                payload = source.read(MAX_CACHE_BLOB_BYTES + 1025)
            if len(payload) > MAX_CACHE_BLOB_BYTES + 1024:
                raise ValueError("cache_compressed_blob_exceeds_policy")
            return payload

        return await asyncio.to_thread(load)

    async def write(self, name: str, payload: bytes) -> None:
        if len(payload) > MAX_CACHE_BLOB_BYTES + 1024:
            raise ValueError("cache_compressed_blob_exceeds_policy")

        def save() -> None:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as target:
                    temporary = target.name
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.path(name))
                temporary = None
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)

        await asyncio.to_thread(save)
