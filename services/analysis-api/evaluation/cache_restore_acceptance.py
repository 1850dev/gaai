"""Restore a read-only shared cache snapshot into isolated temporary storage."""

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path


def manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix != ".gz":
            raise RuntimeError("unexpected_cache_entry")
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify(directory: str) -> None:
    source = Path(directory)
    if not source.is_absolute() or source == Path("/"):
        raise ValueError("specific_cache_directory_required")
    original = manifest(source)
    if not original:
        raise RuntimeError("cache_snapshot_empty")
    with tempfile.TemporaryDirectory(prefix="gaai-restore-acceptance-") as temporary:
        backup, restored = Path(temporary) / "backup", Path(temporary) / "restored"
        backup.mkdir(mode=0o700)
        restored.mkdir(mode=0o700)
        for name in original:
            shutil.copyfile(source / name, backup / name)
            shutil.copyfile(backup / name, restored / name)
        assert manifest(backup) == original == manifest(restored)
        assert manifest(source) == original
        print(
            json.dumps(
                {
                    "cache_blobs": len(original),
                    "backup_restore_hashes_match": True,
                    "source_read_only": True,
                    "metadata_restore_tested": False,
                    "temporary_copies_removed_on_exit": True,
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    verify(parser.parse_args().directory)
