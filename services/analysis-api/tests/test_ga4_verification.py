from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as google_exceptions
from google.auth.credentials import AnonymousCredentials, Credentials

from analytics_ai.api.app import create_app
from analytics_ai.auth.firebase import VerifiedIdentity
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.config import Settings
from analytics_ai.domain.models import DataAccessProfile, DataSourceSet, Ga4AuthMode
from analytics_ai.ga4.credentials import Ga4CredentialContext
from analytics_ai.ga4.verification import (
    Ga4PropertyVerificationService,
    Ga4VerificationError,
)
from analytics_ai.repositories.memory import InMemoryRepository


class StaticVerifier:
    async def verify(self, token: str) -> VerifiedIdentity:
        return VerifiedIdentity(uid=token, email=f"{token}@example.com")


class RecordingCredentials:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def get_credentials(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext:
        auth_mode = source_set.ga4.auth_mode if source_set.ga4 else None
        assert auth_mode is not None
        self.calls.append((user_id, source_set.id, profile.id if profile else None))
        return Ga4CredentialContext(
            credentials=AnonymousCredentials(),
            auth_mode=auth_mode,
            google_subject=(
                "google-user-a" if auth_mode is Ga4AuthMode.USER_OAUTH else None
            ),
            service_account_email=(
                profile.service_account_email
                if auth_mode is Ga4AuthMode.SERVICE_ACCOUNT and profile
                else None
            ),
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
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def property_details(property_id: str = "222222222") -> dict[str, object]:
    return {
        "name": f"properties/{property_id}",
        "display_name": "A社 Web",
        "property_type": "PROPERTY_TYPE_ORDINARY",
        "parent": "accounts/111111111",
        "time_zone": "Asia/Tokyo",
        "currency_code": "JPY",
        "service_level": "GOOGLE_ANALYTICS_360",
    }


@pytest.mark.asyncio
async def test_user_verifies_only_the_registered_property(
    repository: InMemoryRepository,
) -> None:
    credentials = RecordingCredentials()
    ga4 = RecordingGa4(property_details())
    service = Ga4PropertyVerificationService(
        repository,
        credentials=credentials,
        ga4_client=ga4,
    )

    result = await service.verify(
        user_id="user-a",
        data_source_set_id="company_a_web",
        request_id="req_verify_test",
    )

    assert result.property_id == "222222222"
    assert result.service_level == "GOOGLE_ANALYTICS_360"
    assert result.stream_scope_verification == "pending_report_acceptance"
    assert ga4.calls == [("get_property_details", {"property_id": "222222222"})]
    assert credentials.calls == [("user-a", "company_a_web", "dap_company_a")]
    assert repository.audit_events[-1].details["ga4_auth_mode"] == "USER_OAUTH"
    assert repository.ga4_property_access[("user-a", "222222222")].status.value == (
        "accessible"
    )


@pytest.mark.asyncio
async def test_verification_rejects_a_response_for_another_property(
    repository: InMemoryRepository,
) -> None:
    service = Ga4PropertyVerificationService(
        repository,
        credentials=RecordingCredentials(),
        ga4_client=RecordingGa4(property_details("999999999")),
    )

    with pytest.raises(Ga4VerificationError) as rejected:
        await service.verify(
            user_id="user-a",
            data_source_set_id="company_a_web",
            request_id="req_wrong_property",
        )

    assert rejected.value.code == "ga4_registered_property_mismatch"
    assert repository.audit_events[-1].status == "error"


@pytest.mark.asyncio
async def test_missing_data_source_set_grant_prevents_verification(
    repository: InMemoryRepository,
) -> None:
    credentials = RecordingCredentials()
    ga4 = RecordingGa4(property_details())
    service = Ga4PropertyVerificationService(
        repository,
        credentials=credentials,
        ga4_client=ga4,
    )

    with pytest.raises(AccessDenied) as denied:
        await service.verify(
            user_id="user-b",
            data_source_set_id="company_a_web",
            request_id="req_verify_denied",
        )

    assert denied.value.internal_reason == "missing_source_set_grant"
    assert credentials.calls == []
    assert ga4.calls == []


def test_verification_api_is_independently_gated(
    repository: InMemoryRepository,
) -> None:
    service = Ga4PropertyVerificationService(
        repository,
        credentials=RecordingCredentials(),
        ga4_client=RecordingGa4(property_details()),
    )
    app = create_app(
        Settings(environment="test"),
        repository=repository,
        token_verifier=StaticVerifier(),
        ga4_verification=service,
    )

    response = TestClient(app).post(
        "/api/data-source-sets/company_a_web/ga4/verify",
        headers={"Authorization": "Bearer user-a"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "admin_connection_checks_disabled"}}


def test_property_permission_loss_returns_403_and_updates_status(
    repository: InMemoryRepository,
) -> None:
    service = Ga4PropertyVerificationService(
        repository,
        credentials=RecordingCredentials(),
        ga4_client=RecordingGa4(google_exceptions.Forbidden("permission removed")),
    )
    app = create_app(
        Settings(environment="test", enable_admin_connection_checks=True),
        repository=repository,
        token_verifier=StaticVerifier(),
        ga4_verification=service,
    )

    response = TestClient(app).post(
        "/api/data-source-sets/company_a_web/ga4/verify",
        headers={"Authorization": "Bearer user-a"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "ga4_property_access_denied"}}
    assert repository.ga4_property_access[("user-a", "222222222")].status.value == (
        "denied"
    )
