"""Create-only bootstrap planning and Firestore batch persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from google.cloud import firestore

from analytics_ai.bootstrap.models import BootstrapManifest
from analytics_ai.config import Settings
from analytics_ai.domain.models import AuditEvent


@dataclass(frozen=True, slots=True)
class BootstrapDocument:
    path: str
    value: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    audit_event_id: str
    document_count: int


class BootstrapStore(Protocol):
    def create_all(self, documents: tuple[BootstrapDocument, ...]) -> None: ...


def validate_bootstrap_settings(settings: Settings) -> None:
    if settings.environment not in {"local", "dev"}:
        raise ValueError("bootstrap is limited to local and dev environments")
    if settings.resolved_repository_backend != "firestore":
        raise ValueError("bootstrap requires the Firestore repository backend")
    if settings.enable_analytics_tools:
        raise ValueError("disable Analytics Tools before running bootstrap")


class FirestoreBootstrapStore:
    """Persist all records atomically with Firestore create preconditions."""

    def __init__(self, project_id: str) -> None:
        self._client = firestore.Client(project=project_id)

    def create_all(self, documents: tuple[BootstrapDocument, ...]) -> None:
        batch = self._client.batch()
        for document in documents:
            batch.create(self._client.document(document.path), document.value)
        batch.commit()


class DevBootstrapService:
    def __init__(self, settings: Settings, store: BootstrapStore) -> None:
        self._settings = settings
        self._store = store

    def run(self, manifest: BootstrapManifest) -> BootstrapResult:
        validate_bootstrap_settings(self._settings)

        records = self._documents(manifest)
        audit = AuditEvent(
            id=f"audit_{uuid.uuid4().hex}",
            request_id=f"bootstrap_{uuid.uuid4().hex}",
            action="development.bootstrap.create",
            user_id=manifest.actor_user_id,
            company_id=manifest.company_id,
            status="success",
            details={
                "reason": manifest.reason,
                "create_only": True,
                "record_count": len(records),
                "resource_types": self._resource_types(manifest),
            },
        )
        documents = (*records, self._document("auditEvents", audit.id, audit))
        paths = [document.path for document in documents]
        if len(paths) != len(set(paths)):
            raise ValueError("bootstrap document paths must be unique")
        self._store.create_all(documents)
        return BootstrapResult(
            audit_event_id=audit.id,
            document_count=len(documents),
        )

    @staticmethod
    def _document(collection: str, record_id: str, model: Any) -> BootstrapDocument:
        return BootstrapDocument(
            path=f"{collection}/{record_id}",
            value=model.model_dump(mode="json"),
        )

    def _documents(self, manifest: BootstrapManifest) -> tuple[BootstrapDocument, ...]:
        return (
            *(self._document("users", user.uid, user) for user in manifest.users),
            *(
                self._document("companies", company.id, company)
                for company in manifest.companies
            ),
            *(
                BootstrapDocument(
                    path=(f"companies/{member.company_id}/members/{member.user_id}"),
                    value=member.model_dump(mode="json"),
                )
                for member in manifest.company_members
            ),
            *(
                self._document("dataAccessProfiles", profile.id, profile)
                for profile in manifest.data_access_profiles
            ),
            *(
                self._document("dataSourceSets", source_set.id, source_set)
                for source_set in manifest.data_source_sets
            ),
            *(
                self._document("userAccessGrants", grant.id, grant)
                for grant in manifest.user_access_grants
            ),
            *(
                self._document("semanticDefinitions", definition.id, definition)
                for definition in manifest.semantic_definitions
            ),
        )

    @staticmethod
    def _resource_types(manifest: BootstrapManifest) -> list[str]:
        optional = (
            ("user", manifest.users),
            ("company", manifest.companies),
            ("company_member", manifest.company_members),
            ("data_access_profile", manifest.data_access_profiles),
            ("data_source_set", manifest.data_source_sets),
            ("user_access_grant", manifest.user_access_grants),
            ("semantic_definition", manifest.semantic_definitions),
        )
        return [name for name, records in optional if records]
