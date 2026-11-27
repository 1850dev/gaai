"""Backend-only Google OAuth authorization for per-user GA4 access."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import urllib.parse
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from pydantic import Field

from analytics_ai.auth.firebase import VerifiedIdentity
from analytics_ai.config import Settings
from analytics_ai.credentials.token_cipher import TokenCipher
from analytics_ai.domain.models import (
    AuditEvent,
    FrozenModel,
    Ga4ConnectionStatus,
    Ga4OAuthTransaction,
    Ga4UserConnection,
    Status,
)
from analytics_ai.repositories.protocols import ApplicationRepository

ANALYTICS_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
OAUTH_SCOPES = frozenset({"openid", "email", "profile", ANALYTICS_READONLY_SCOPE})
REQUIRED_GRANTED_SCOPES = frozenset({ANALYTICS_READONLY_SCOPE})
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"


class Ga4OAuthError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Ga4ConnectResponse(FrozenModel):
    authorization_url: str


class Ga4ConnectionSummary(FrozenModel):
    status: Ga4ConnectionStatus
    google_email: str | None = None
    granted_scopes: frozenset[str] = Field(default_factory=frozenset)
    connected_at: datetime | None = None
    last_verified_at: datetime | None = None


class OAuthTokenSet(FrozenModel):
    refresh_token: str
    id_token: str
    granted_scopes: frozenset[str]


class OAuthIdentity(FrozenModel):
    subject: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    email_verified: bool


class OAuthTransport(Protocol):
    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenSet: ...

    async def revoke(self, token: str) -> None: ...


class OAuthIdentityVerifier(Protocol):
    async def verify(self, raw_id_token: str) -> OAuthIdentity: ...


class GoogleOAuthTransport:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> OAuthTokenSet:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._settings.ga4_oauth_client_id,
                    "client_secret": (
                        self._settings.ga4_oauth_client_secret.get_secret_value()
                    ),
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
        if response.status_code >= 400:
            raise Ga4OAuthError("ga4_oauth_code_exchange_failed")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise Ga4OAuthError("ga4_oauth_token_response_invalid")
        refresh_token = payload.get("refresh_token")
        raw_id_token = payload.get("id_token")
        raw_scopes = payload.get("scope")
        if (
            not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(raw_id_token, str)
            or not raw_id_token
            or not isinstance(raw_scopes, str)
        ):
            raise Ga4OAuthError("ga4_oauth_offline_grant_missing")
        return OAuthTokenSet(
            refresh_token=refresh_token,
            id_token=raw_id_token,
            granted_scopes=frozenset(raw_scopes.split()),
        )

    async def revoke(self, token: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(REVOKE_ENDPOINT, data={"token": token})
        if response.status_code not in {200, 400}:
            response.raise_for_status()


class GoogleOAuthIdentityVerifier:
    def __init__(self, client_id: str) -> None:
        self._client_id = client_id

    async def verify(self, raw_id_token: str) -> OAuthIdentity:
        claims = await asyncio.to_thread(
            google_id_token.verify_oauth2_token,
            raw_id_token,
            GoogleAuthRequest(),
            self._client_id,
        )
        verified = cast(Mapping[str, object], claims)
        subject = verified.get("sub")
        email = verified.get("email")
        if not isinstance(subject, str) or not isinstance(email, str):
            raise Ga4OAuthError("ga4_oauth_identity_invalid")
        return OAuthIdentity(
            subject=subject,
            email=email,
            email_verified=verified.get("email_verified") is True,
        )


class Ga4OAuthService:
    def __init__(
        self,
        settings: Settings,
        repository: ApplicationRepository,
        cipher: TokenCipher,
        *,
        transport: OAuthTransport | None = None,
        identity_verifier: OAuthIdentityVerifier | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._cipher = cipher
        self._transport = transport or GoogleOAuthTransport(settings)
        self._identity_verifier = identity_verifier or GoogleOAuthIdentityVerifier(
            settings.ga4_oauth_client_id
        )

    async def status(self, user_id: str) -> Ga4ConnectionSummary:
        connection = await self._repository.get_ga4_user_connection(user_id)
        if connection is None:
            return Ga4ConnectionSummary(status=Ga4ConnectionStatus.DISCONNECTED)
        return Ga4ConnectionSummary(
            status=connection.status,
            google_email=connection.google_email,
            granted_scopes=connection.granted_scopes,
            connected_at=connection.connected_at,
            last_verified_at=connection.last_verified_at,
        )

    async def connect(self, identity: VerifiedIdentity) -> Ga4ConnectResponse:
        if not self._settings.enable_ga4_user_oauth:
            raise Ga4OAuthError("ga4_user_oauth_disabled")
        user = await self._repository.get_user(identity.uid)
        if user is None or user.status is not Status.ACTIVE:
            raise Ga4OAuthError("ga4_oauth_access_denied")
        if not identity.email:
            raise Ga4OAuthError("firebase_google_email_required")
        if not identity.google_subject:
            raise Ga4OAuthError("firebase_google_identity_required")
        state = secrets.token_urlsafe(48)
        verifier = secrets.token_urlsafe(64)
        transaction = Ga4OAuthTransaction(
            state_hash=self._state_hash(state),
            user_id=identity.uid,
            expected_email=identity.email.casefold(),
            expected_google_subject=identity.google_subject,
            code_verifier=verifier,
            redirect_uri=self._settings.ga4_oauth_redirect_uri,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self._settings.ga4_oauth_transaction_ttl_seconds),
        )
        await self._repository.create_ga4_oauth_transaction(transaction)
        query = urllib.parse.urlencode(
            {
                "access_type": "offline",
                "client_id": self._settings.ga4_oauth_client_id,
                "code_challenge": self._pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "include_granted_scopes": "true",
                "login_hint": identity.email,
                "prompt": "consent",
                "redirect_uri": self._settings.ga4_oauth_redirect_uri,
                "response_type": "code",
                "scope": " ".join(sorted(OAUTH_SCOPES)),
                "state": state,
            }
        )
        await self._audit(identity.uid, "ga4.oauth.connect.started", "success")
        return Ga4ConnectResponse(authorization_url=f"{AUTHORIZATION_ENDPOINT}?{query}")

    async def callback(self, *, state: str, code: str) -> Ga4UserConnection:
        transaction = await self._repository.consume_ga4_oauth_transaction(
            self._state_hash(state)
        )
        if transaction is None or transaction.expires_at <= datetime.now(UTC):
            raise Ga4OAuthError("ga4_oauth_state_invalid_or_expired")
        try:
            token_set = await self._transport.exchange_code(
                code=code,
                code_verifier=transaction.code_verifier,
                redirect_uri=transaction.redirect_uri,
            )
            if not REQUIRED_GRANTED_SCOPES.issubset(token_set.granted_scopes):
                raise Ga4OAuthError("ga4_oauth_required_scopes_missing")
            oauth_identity = await self._identity_verifier.verify(token_set.id_token)
            if not oauth_identity.email_verified:
                raise Ga4OAuthError("ga4_oauth_email_not_verified")
            if oauth_identity.email.casefold() != transaction.expected_email:
                raise Ga4OAuthError("ga4_oauth_identity_mismatch")
            if oauth_identity.subject != transaction.expected_google_subject:
                raise Ga4OAuthError("ga4_oauth_identity_mismatch")
            ciphertext = await self._cipher.encrypt(
                token_set.refresh_token,
                user_id=transaction.user_id,
            )
            now = datetime.now(UTC)
            connection = Ga4UserConnection(
                user_id=transaction.user_id,
                google_subject=oauth_identity.subject,
                google_email=oauth_identity.email,
                granted_scopes=token_set.granted_scopes,
                encrypted_refresh_token=ciphertext,
                connected_at=now,
                last_verified_at=now,
                token_created_at=now,
            )
            await self._repository.put_ga4_user_connection(connection)
        except Exception as error:
            code_value = (
                error.code if isinstance(error, Ga4OAuthError) else type(error).__name__
            )
            await self._audit(
                transaction.user_id,
                "ga4.oauth.callback",
                "error",
                error_code=code_value,
            )
            if isinstance(error, Ga4OAuthError):
                raise
            raise Ga4OAuthError("ga4_oauth_callback_failed") from error
        await self._audit(
            transaction.user_id,
            "ga4.oauth.callback",
            "success",
            google_subject=connection.google_subject,
            granted_scopes=sorted(connection.granted_scopes),
        )
        return connection

    async def cancel(self, *, state: str, provider_error: str) -> None:
        transaction = await self._repository.consume_ga4_oauth_transaction(
            self._state_hash(state)
        )
        if transaction is None or transaction.expires_at <= datetime.now(UTC):
            raise Ga4OAuthError("ga4_oauth_state_invalid_or_expired")
        await self._audit(
            transaction.user_id,
            "ga4.oauth.callback",
            "denied",
            error_code="ga4_oauth_denied",
            provider_error=(
                provider_error
                if provider_error
                in {
                    "access_denied",
                    "admin_policy_enforced",
                    "disallowed_useragent",
                    "org_internal",
                    "redirect_uri_mismatch",
                }
                else "unknown"
            ),
        )

    async def disconnect(self, user_id: str) -> None:
        connection = await self._repository.get_ga4_user_connection(user_id)
        if connection is None:
            return
        revoke_status = "not_attempted"
        ciphertext = connection.encrypted_refresh_token
        if ciphertext:
            try:
                refresh_token = await self._cipher.decrypt(ciphertext, user_id=user_id)
                await self._transport.revoke(refresh_token)
                revoke_status = "success"
            except Exception:
                revoke_status = "failed"
        disconnected = connection.model_copy(
            update={
                "encrypted_refresh_token": None,
                "status": Ga4ConnectionStatus.DISCONNECTED,
                "last_verified_at": datetime.now(UTC),
            }
        )
        await self._repository.put_ga4_user_connection(disconnected)
        await self._audit(
            user_id,
            "ga4.oauth.disconnect",
            "success",
            revoke_status=revoke_status,
        )

    async def _audit(
        self,
        user_id: str,
        action: str,
        status: str,
        **details: object,
    ) -> None:
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=f"oauth_{uuid.uuid4().hex}",
                action=action,
                user_id=user_id,
                status=status,
                details={"ga4_auth_mode": "USER_OAUTH", **details},
            )
        )

    @staticmethod
    def _state_hash(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
