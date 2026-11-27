from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from analytics_ai.bootstrap.models import BootstrapManifest
from analytics_ai.bootstrap.service import BootstrapDocument, DevBootstrapService
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    Company,
    CompanyMember,
    ConnectionStatus,
    DataAccessProfile,
    DataSourceSet,
    Ga4Source,
    SemanticDefinition,
    SemanticScope,
    Status,
    User,
    UserAccessGrant,
)


class CapturingStore:
    def __init__(self) -> None:
        self.documents: tuple[BootstrapDocument, ...] = ()

    def create_all(self, documents: tuple[BootstrapDocument, ...]) -> None:
        self.documents = documents


def manifest_payload() -> dict[str, object]:
    return {
        "actor_user_id": "firebase-user",
        "company_id": "company_dev",
        "users": (User(uid="firebase-user", email="dev@example.com"),),
        "companies": (Company(id="company_dev", name="Development Company"),),
        "company_members": (
            CompanyMember(
                user_id="firebase-user",
                company_id="company_dev",
            ),
        ),
        "data_access_profiles": (
            DataAccessProfile(
                id="profile_dev",
                company_id="company_dev",
                service_account_email=(
                    "company-dev@analytics-ai-dev.iam.gserviceaccount.com"
                ),
            ),
        ),
        "data_source_sets": (
            DataSourceSet(
                id="company_dev_web",
                company_id="company_dev",
                name="Development Web",
                data_access_profile_id="profile_dev",
                ga4=Ga4Source(property_id="123456789", display_name="Dev Web"),
            ),
        ),
        "user_access_grants": (
            UserAccessGrant(
                id="grant_dev_web",
                user_id="firebase-user",
                data_source_set_id="company_dev_web",
            ),
        ),
        "semantic_definitions": (
            SemanticDefinition(
                id="semantic_dev_cv",
                canonical_name="CV",
                aliases=("コンバージョン",),
                kind="key_event",
                scope=SemanticScope.DATA_SOURCE_SET,
                company_id="company_dev",
                data_source_set_id="company_dev_web",
                ga4_mapping={"event_name": "generate_lead"},
                version="dev-v1",
            ),
        ),
        "reason": "Initialize isolated development access records",
    }


def test_bootstrap_creates_all_records_and_audit_in_one_call() -> None:
    manifest = BootstrapManifest.model_validate(manifest_payload())
    store = CapturingStore()
    result = DevBootstrapService(
        Settings(
            environment="local",
            repository_backend="firestore",
            enable_analytics_tools=False,
        ),
        store,
    ).run(manifest)

    paths = {document.path for document in store.documents}
    assert {
        "users/firebase-user",
        "companies/company_dev",
        "companies/company_dev/members/firebase-user",
        "dataAccessProfiles/profile_dev",
        "dataSourceSets/company_dev_web",
        "userAccessGrants/grant_dev_web",
        "semanticDefinitions/semantic_dev_cv",
    }.issubset(paths)
    assert any(path.startswith("auditEvents/audit_") for path in paths)
    assert result.document_count == 8


def test_bootstrap_allows_staged_create_only_manifests() -> None:
    manifest = BootstrapManifest(
        actor_user_id="firebase-user",
        company_id="company_dev",
        data_access_profiles=(
            DataAccessProfile(
                id="profile_dev",
                company_id="company_dev",
                service_account_email=(
                    "company-dev@analytics-ai-dev.iam.gserviceaccount.com"
                ),
            ),
        ),
        reason="Add a disabled data access profile after identity bootstrap",
    )
    assert manifest.users == ()


def test_bootstrap_rejects_preverified_profile() -> None:
    payload = manifest_payload()
    payload["data_access_profiles"] = (
        DataAccessProfile(
            id="profile_dev",
            company_id="company_dev",
            service_account_email=(
                "company-dev@analytics-ai-dev.iam.gserviceaccount.com"
            ),
            status=Status.ACTIVE,
            connection_status=ConnectionStatus.VERIFIED,
        ),
    )
    with pytest.raises(ValidationError, match="cannot mark a profile"):
        BootstrapManifest.model_validate(payload)


def test_bootstrap_refuses_memory_repository() -> None:
    service = DevBootstrapService(
        Settings(environment="local", repository_backend="memory"),
        CapturingStore(),
    )
    with pytest.raises(ValueError, match="requires the Firestore"):
        service.run(BootstrapManifest.model_validate(manifest_payload()))


def test_bootstrap_refuses_non_development_environment() -> None:
    service = DevBootstrapService(
        Settings(environment="stg", repository_backend="firestore"),
        CapturingStore(),
    )
    with pytest.raises(ValueError, match="limited to local and dev"):
        service.run(BootstrapManifest.model_validate(manifest_payload()))


@pytest.mark.parametrize(
    "filename",
    ["bootstrap.identity.example.json", "bootstrap.data-source.example.json"],
)
def test_documented_bootstrap_manifest_is_valid(filename: str) -> None:
    repository_root = Path(__file__).parents[3]
    payload = (repository_root / "docs" / "examples" / filename).read_text(
        encoding="utf-8"
    )
    BootstrapManifest.model_validate_json(payload)
