from __future__ import annotations

# Test-only sentinel values intentionally exercise secret-handling boundaries.
# ruff: noqa: S105, S106
import urllib.parse

import pytest

from analytics_ai.auth.firebase import VerifiedIdentity
from analytics_ai.config import Settings
from analytics_ai.domain.models import User
from analytics_ai.ga4.oauth import (
    ANALYTICS_READONLY_SCOPE,
    OAUTH_SCOPES,
    Ga4OAuthError,
    Ga4OAuthService,
    OAuthIdentity,
    OAuthTokenSet,
)
from analytics_ai.repositories.memory import InMemoryRepository


class RecordingCipher:
    def __init__(self) -> None:
        self.encrypt_calls: list[tuple[str, str]] = []
        self.decrypt_calls: list[tuple[str, str]] = []

    async def encrypt(self, plaintext: str, *, user_id: str) -> str:
        self.encrypt_calls.append((plaintext, user_id))
        return f"kms-ciphertext-for-{user_id}"

    async def decrypt(self, ciphertext: str, *, user_id: str) -> str:
        self.decrypt_calls.append((ciphertext, user_id))
        return "refresh-token-secret"


class RecordingTransport:
    def __init__(
        self,
        granted_scopes: frozenset[str] = OAUTH_SCOPES,
    ) -> None:
        self.exchange_calls: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.granted_scopes = granted_scopes

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenSet:
        self.exchange_calls.append(
            {
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )
        return OAuthTokenSet(
            refresh_token="refresh-token-secret",
            id_token="signed-google-id-token",
            granted_scopes=self.granted_scopes,
        )

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)


class StaticIdentityVerifier:
    def __init__(
        self,
        *,
        subject: str = "google-subject-a",
        email: str = "user-a@example.com",
    ) -> None:
        self.subject = subject
        self.email = email

    async def verify(self, raw_id_token: str) -> OAuthIdentity:
        assert raw_id_token == "signed-google-id-token"
        return OAuthIdentity(
            subject=self.subject,
            email=self.email,
            email_verified=True,
        )


def oauth_settings() -> Settings:
    return Settings(
        environment="test",
        enable_ga4_user_oauth=True,
        ga4_oauth_client_id="test.apps.googleusercontent.com",
        ga4_oauth_client_secret="client-secret",
        ga4_oauth_redirect_uri=(
            "https://api.example.com/api/integrations/ga4/callback"
        ),
        ga4_oauth_return_url="https://app.example.com/analytics",
        ga4_oauth_kms_key_name=(
            "projects/test/locations/global/keyRings/ga4/cryptoKeys/oauth"
        ),
    )


def firebase_identity() -> VerifiedIdentity:
    return VerifiedIdentity(
        uid="user-a",
        email="user-a@example.com",
        google_subject="google-subject-a",
    )


def state_from(authorization_url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(authorization_url).query)
    return query["state"][0]


def make_service(
    repository: InMemoryRepository,
    *,
    cipher: RecordingCipher | None = None,
    transport: RecordingTransport | None = None,
    identity_verifier: StaticIdentityVerifier | None = None,
) -> tuple[Ga4OAuthService, RecordingCipher, RecordingTransport]:
    repository.users.setdefault(
        "user-a",
        User(uid="user-a", email="user-a@example.com"),
    )
    selected_cipher = cipher or RecordingCipher()
    selected_transport = transport or RecordingTransport()
    return (
        Ga4OAuthService(
            oauth_settings(),
            repository,
            selected_cipher,
            transport=selected_transport,
            identity_verifier=identity_verifier or StaticIdentityVerifier(),
        ),
        selected_cipher,
        selected_transport,
    )


@pytest.mark.asyncio
async def test_connect_uses_backend_code_flow_offline_state_and_pkce() -> None:
    repository = InMemoryRepository()
    service, _, _ = make_service(repository)

    response = await service.connect(firebase_identity())
    parsed = urllib.parse.urlparse(response.authorization_url)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert query["prompt"] == ["consent"]
    assert ANALYTICS_READONLY_SCOPE in query["scope"][0].split()
    assert len(query["state"][0]) >= 43
    assert query["state"][0] not in repository.ga4_oauth_transactions
    assert len(repository.ga4_oauth_transactions) == 1


@pytest.mark.asyncio
async def test_callback_binds_identity_and_persists_only_ciphertext() -> None:
    repository = InMemoryRepository()
    service, cipher, transport = make_service(repository)
    connect = await service.connect(firebase_identity())
    state = state_from(connect.authorization_url)

    connection = await service.callback(state=state, code="authorization-code")

    assert connection.google_subject == "google-subject-a"
    assert connection.google_email == "user-a@example.com"
    assert connection.encrypted_refresh_token == "kms-ciphertext-for-user-a"
    assert cipher.encrypt_calls == [("refresh-token-secret", "user-a")]
    assert transport.exchange_calls[0]["code"] == "authorization-code"
    assert transport.exchange_calls[0]["code_verifier"]
    persisted = repository.ga4_user_connections["user-a"]
    assert persisted.encrypted_refresh_token == "kms-ciphertext-for-user-a"
    assert "refresh-token-secret" not in persisted.model_dump_json()
    assert all(
        "refresh-token-secret" not in event.model_dump_json()
        for event in repository.audit_events
    )


@pytest.mark.asyncio
async def test_callback_accepts_google_normalized_openid_scopes() -> None:
    repository = InMemoryRepository()
    transport = RecordingTransport(
        frozenset(
            {
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                ANALYTICS_READONLY_SCOPE,
            }
        )
    )
    service, _, _ = make_service(repository, transport=transport)
    connect = await service.connect(firebase_identity())

    connection = await service.callback(
        state=state_from(connect.authorization_url),
        code="authorization-code",
    )

    assert ANALYTICS_READONLY_SCOPE in connection.granted_scopes


@pytest.mark.asyncio
async def test_callback_state_is_single_use() -> None:
    repository = InMemoryRepository()
    service, _, _ = make_service(repository)
    connect = await service.connect(firebase_identity())
    state = state_from(connect.authorization_url)
    await service.callback(state=state, code="authorization-code")

    with pytest.raises(Ga4OAuthError, match="ga4_oauth_state_invalid_or_expired"):
        await service.callback(state=state, code="authorization-code")


@pytest.mark.asyncio
async def test_denied_callback_consumes_state_without_storing_credentials() -> None:
    repository = InMemoryRepository()
    service, _, transport = make_service(repository)
    connect = await service.connect(firebase_identity())
    state = state_from(connect.authorization_url)

    await service.cancel(state=state, provider_error="access_denied")

    assert repository.ga4_oauth_transactions == {}
    assert repository.ga4_user_connections == {}
    assert transport.exchange_calls == []
    assert repository.audit_events[-1].status == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "email"),
    [
        ("google-subject-other", "user-a@example.com"),
        ("google-subject-a", "other@example.com"),
    ],
)
async def test_callback_rejects_a_different_google_account(
    subject: str,
    email: str,
) -> None:
    repository = InMemoryRepository()
    service, cipher, _ = make_service(
        repository,
        identity_verifier=StaticIdentityVerifier(subject=subject, email=email),
    )
    connect = await service.connect(firebase_identity())
    state = state_from(connect.authorization_url)

    with pytest.raises(Ga4OAuthError, match="ga4_oauth_identity_mismatch"):
        await service.callback(state=state, code="authorization-code")

    assert repository.ga4_user_connections == {}
    assert cipher.encrypt_calls == []


@pytest.mark.asyncio
async def test_disconnect_revokes_and_removes_persisted_refresh_token() -> None:
    repository = InMemoryRepository()
    service, cipher, transport = make_service(repository)
    connect = await service.connect(firebase_identity())
    state = state_from(connect.authorization_url)
    await service.callback(state=state, code="authorization-code")

    await service.disconnect("user-a")

    disconnected = repository.ga4_user_connections["user-a"]
    assert disconnected.status.value == "disconnected"
    assert disconnected.encrypted_refresh_token is None
    assert cipher.decrypt_calls == [("kms-ciphertext-for-user-a", "user-a")]
    assert transport.revoked == ["refresh-token-secret"]


@pytest.mark.asyncio
async def test_connect_requires_firebase_google_identity() -> None:
    repository = InMemoryRepository()
    service, _, _ = make_service(repository)

    with pytest.raises(Ga4OAuthError, match="firebase_google_identity_required"):
        await service.connect(
            VerifiedIdentity(uid="user-a", email="user-a@example.com")
        )

    assert repository.ga4_oauth_transactions == {}
