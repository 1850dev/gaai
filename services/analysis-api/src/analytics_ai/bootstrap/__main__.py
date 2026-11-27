"""CLI entry point for audited, create-only development bootstrap."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import DefaultCredentialsError
from pydantic import ValidationError

from analytics_ai.bootstrap.models import BootstrapManifest
from analytics_ai.bootstrap.service import (
    DevBootstrapService,
    FirestoreBootstrapStore,
    validate_bootstrap_settings,
)
from analytics_ai.config import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to a private bootstrap JSON manifest.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> BootstrapManifest:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    return BootstrapManifest.model_validate(payload)


def main() -> int:
    args = parse_args()
    try:
        settings = Settings()
        validate_bootstrap_settings(settings)
        manifest = load_manifest(args.manifest)
        store = FirestoreBootstrapStore(settings.firebase_project_id)
        result = DevBootstrapService(settings, store).run(manifest)
    except (
        DefaultCredentialsError,
        GoogleAPICallError,
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise SystemExit(f"Bootstrap refused: {error}") from error
    print(
        f"Bootstrap created {result.document_count} documents; "
        f"audit event: {result.audit_event_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
