"""Explicit request-scoped GA4 credential selection without automatic fallback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from google.auth import credentials as google_credentials
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials

from analytics_ai.config import Settings
from analytics_ai.credentials.broker import (
    ANALYTICS_READONLY_SCOPE,
    CredentialBroker,
)
from analytics_ai.credentials.token_cipher import TokenCipher
from analytics_ai.domain.models import (
    DataAccessProfile,
    DataSourceSet,
    Ga4AuthMode,
    Ga4ConnectionStatus,
)
from analytics_ai.repositories.protocols import ApplicationRepository


class Ga4CredentialError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ServiceAccountGa4CredentialProvider(Protocol):
    async def for_ga4(
        self, profile: DataAccessProfile
    ) -> google_credentials.Credentials: ...


@dataclass(frozen=True, slots=True)
class Ga4CredentialContext:
    credentials: google_credentials.Credentials
    auth_mode: Ga4AuthMode
    google_subject: str | None = None
    google_email: str | None = None
    granted_scopes: frozenset[str] = frozenset()
    service_account_email: str | None = None
    data_access_profile_id: str | None = None
    token_created_at: datetime | None = None
    token_refresh_at: datetime | None = None


class Ga4ExecutionCredentialProvider(Protocol):
    async def get_credentials(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext: ...


class Ga4CredentialProvider:
    def __init__(
        self,
        settings: Settings,
        repository: ApplicationRepository,
        cipher: TokenCipher | None,
        *,
        service_accounts: ServiceAccountGa4CredentialProvider | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._cipher = cipher
        self._service_accounts = service_accounts or CredentialBroker()

    async def get_credentials(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext:
        source = source_set.ga4
        if source is None:
            raise Ga4CredentialError("ga4_not_registered")
        if source.auth_mode is Ga4AuthMode.SERVICE_ACCOUNT:
            if profile is None or not profile.ready:
                raise Ga4CredentialError("ga4_service_account_profile_unavailable")
            credentials = await self._service_accounts.for_ga4(profile)
            return Ga4CredentialContext(
                credentials=credentials,
                auth_mode=source.auth_mode,
                service_account_email=profile.service_account_email,
                data_access_profile_id=profile.id,
            )

        return await self.get_user_credentials(user_id=user_id)

    async def get_user_credentials(self, *, user_id: str) -> Ga4CredentialContext:
        """Return only this Firebase user's explicitly connected Google identity."""

        connection = await self._repository.get_ga4_user_connection(user_id)
        if (
            connection is None
            or connection.status is not Ga4ConnectionStatus.CONNECTED
            or connection.encrypted_refresh_token is None
        ):
            raise Ga4CredentialError("ga4_user_connection_required")
        if self._cipher is None or not self._settings.enable_ga4_user_oauth:
            raise Ga4CredentialError("ga4_user_oauth_disabled")
        if ANALYTICS_READONLY_SCOPE not in connection.granted_scopes:
            raise Ga4CredentialError("ga4_oauth_required_scopes_missing")
        refresh_token = await self._cipher.decrypt(
            connection.encrypted_refresh_token,
            user_id=user_id,
        )
        credentials = UserCredentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
            client_id=self._settings.ga4_oauth_client_id,
            client_secret=self._settings.ga4_oauth_client_secret.get_secret_value(),
            scopes=[ANALYTICS_READONLY_SCOPE],
        )
        try:
            await asyncio.to_thread(credentials.refresh, Request())
        except RefreshError as error:
            await self._repository.put_ga4_user_connection(
                connection.model_copy(
                    update={
                        "status": Ga4ConnectionStatus.REAUTHORIZATION_REQUIRED,
                        "encrypted_refresh_token": None,
                    }
                )
            )
            raise Ga4CredentialError("ga4_reauthorization_required") from error
        except Exception as error:
            raise Ga4CredentialError("ga4_credential_refresh_failed") from error
        refreshed_at = datetime.now(connection.last_verified_at.tzinfo)
        refreshed_connection = connection.model_copy(
            update={
                "last_verified_at": refreshed_at,
                "token_refresh_at": refreshed_at,
            }
        )
        await self._repository.put_ga4_user_connection(refreshed_connection)
        return Ga4CredentialContext(
            credentials=credentials,
            auth_mode=Ga4AuthMode.USER_OAUTH,
            google_subject=connection.google_subject,
            google_email=connection.google_email,
            granted_scopes=connection.granted_scopes,
            token_created_at=connection.token_created_at,
            token_refresh_at=refreshed_at,
        )
