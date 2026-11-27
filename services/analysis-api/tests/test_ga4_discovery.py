from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials, Credentials

from analytics_ai.api.app import create_app
from analytics_ai.auth.firebase import VerifiedIdentity
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.config import Settings
from analytics_ai.domain.models import Ga4AuthMode
from analytics_ai.ga4.credentials import Ga4CredentialContext
from analytics_ai.ga4.discovery import Ga4PropertyDiscoveryService, Ga4PropertyType
from analytics_ai.repositories.memory import InMemoryRepository


class StaticVerifier:
    async def verify(self, token: str) -> VerifiedIdentity:
        return VerifiedIdentity(
            uid=token,
            email=f"{token}@example.com",
            google_subject=f"google-{token}",
        )


class RecordingCredentials:
    def __init__(self) -> None:
        self.user_ids: list[str] = []

    async def get_user_credentials(self, *, user_id: str) -> Ga4CredentialContext:
        self.user_ids.append(user_id)
        return Ga4CredentialContext(
            credentials=AnonymousCredentials(),
            auth_mode=Ga4AuthMode.USER_OAUTH,
            google_subject=f"google-{user_id}",
            google_email=f"{user_id}@example.com",
        )


class RecordingGa4:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: Credentials,
    ) -> object:
        assert isinstance(credentials, AnonymousCredentials)
        self.calls.append((tool_name, dict(arguments)))
        return self.result


def account_summaries() -> list[dict[str, object]]:
    return [
        {
            "account": "accounts/111111111",
            "display_name": "Example Account",
            "property_summaries": [
                {
                    "property": "properties/100000001",
                    "display_name": "Example Site Overall",
                    "property_type": "PROPERTY_TYPE_ORDINARY",
                    "parent": "accounts/111111111",
                    "can_edit": False,
                },
                {
                    "property": "properties/100000002",
                    "display_name": "サブプロパティ",
                    "property_type": "PROPERTY_TYPE_SUBPROPERTY",
                    "parent": "properties/100000001",
                    "can_edit": True,
                },
            ],
        },
        {
            "account": "accounts/222222222",
            "display_name": "別アカウント",
            "property_summaries": [
                {
                    "property": "properties/999999999",
                    "display_name": "統合プロパティ",
                    "property_type": "PROPERTY_TYPE_ROLLUP",
                    "parent": "accounts/222222222",
                    "can_edit": False,
                }
            ],
        },
    ]


def oauth_settings() -> Settings:
    return Settings(
        environment="test",
        enable_ga4_user_oauth=True,
        ga4_oauth_client_id="test.apps.googleusercontent.com",
        ga4_oauth_client_secret="test-secret",  # noqa: S106
        ga4_oauth_kms_key_name=(
            "projects/test/locations/global/keyRings/ga4/cryptoKeys/oauth"
        ),
    )


@pytest.mark.asyncio
async def test_user_discovers_only_their_google_visible_properties(
    repository: InMemoryRepository,
) -> None:
    credentials = RecordingCredentials()
    ga4 = RecordingGa4(account_summaries())
    service = Ga4PropertyDiscoveryService(
        repository,
        credentials=credentials,
        ga4_client=ga4,
    )

    candidates = await service.discover(
        user_id="user-a",
        request_id="req_discovery_test",
    )

    assert {item.property_id for item in candidates} == {
        "100000001",
        "100000002",
        "999999999",
    }
    by_id = {item.property_id: item for item in candidates}
    assert by_id["100000002"].property_type is Ga4PropertyType.SUBPROPERTY
    assert by_id["999999999"].property_type is Ga4PropertyType.ROLLUP
    assert credentials.user_ids == ["user-a"]
    assert ga4.calls == [("get_account_summaries", {})]
    assert repository.source_sets["company_a_web"].ga4 is not None
    assert repository.source_sets["company_a_web"].ga4.property_id == "222222222"
    assert repository.audit_events[-1].details["ga4_auth_mode"] == "USER_OAUTH"


@pytest.mark.asyncio
async def test_unregistered_user_cannot_discover_properties(
    repository: InMemoryRepository,
) -> None:
    credentials = RecordingCredentials()
    ga4 = RecordingGa4(account_summaries())
    service = Ga4PropertyDiscoveryService(
        repository,
        credentials=credentials,
        ga4_client=ga4,
    )

    with pytest.raises(AccessDenied) as denied:
        await service.discover(user_id="unknown", request_id="req_denied_test")

    assert denied.value.internal_reason == "inactive_or_unregistered_user"
    assert credentials.user_ids == []
    assert ga4.calls == []


def test_discovery_api_remains_disabled_by_user_oauth_feature_gate(
    repository: InMemoryRepository,
) -> None:
    service = Ga4PropertyDiscoveryService(
        repository,
        credentials=RecordingCredentials(),
        ga4_client=RecordingGa4(account_summaries()),
    )
    app = create_app(
        Settings(environment="test"),
        repository=repository,
        token_verifier=StaticVerifier(),
        ga4_discovery=service,
    )

    response = TestClient(app).get(
        "/api/integrations/ga4/properties",
        headers={"Authorization": "Bearer user-a"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "ga4_user_oauth_disabled"}}


def test_user_discovery_api_returns_normalized_candidates(
    repository: InMemoryRepository,
) -> None:
    service = Ga4PropertyDiscoveryService(
        repository,
        credentials=RecordingCredentials(),
        ga4_client=RecordingGa4(account_summaries()),
    )
    app = create_app(
        oauth_settings(),
        repository=repository,
        token_verifier=StaticVerifier(),
        ga4_discovery=service,
    )

    response = TestClient(app).get(
        "/api/integrations/ga4/properties",
        headers={"Authorization": "Bearer user-a"},
    )

    assert response.status_code == 200
    assert {item["property_id"] for item in response.json()} == {
        "100000001",
        "100000002",
        "999999999",
    }
