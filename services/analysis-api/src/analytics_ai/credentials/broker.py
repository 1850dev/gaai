"""Keyless, request-scoped service-account impersonation."""

from __future__ import annotations

import asyncio

import google.auth
from google.auth import credentials as google_credentials
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request

from analytics_ai.domain.models import DataAccessProfile

ANALYTICS_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery"


class CredentialBroker:
    """Create a new impersonated credential per request and profile."""

    def __init__(self, lifetime_seconds: int = 900) -> None:
        if not 300 <= lifetime_seconds <= 3600:
            raise ValueError("credential lifetime must be between 5 and 60 minutes")
        self._lifetime_seconds = lifetime_seconds

    async def for_ga4(
        self, profile: DataAccessProfile
    ) -> google_credentials.Credentials:
        return await self._impersonate(
            profile,
            (ANALYTICS_READONLY_SCOPE,),
        )

    async def for_bigquery(
        self, profile: DataAccessProfile
    ) -> google_credentials.Credentials:
        return await self._impersonate(
            profile,
            (BIGQUERY_SCOPE, CLOUD_PLATFORM_SCOPE),
        )

    async def _impersonate(
        self,
        profile: DataAccessProfile,
        scopes: tuple[str, ...],
    ) -> google_credentials.Credentials:
        if not profile.ready:
            raise ValueError("cannot issue credentials for an unverified profile")

        def build() -> google_credentials.Credentials:
            source, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            return impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
                source_credentials=source,
                target_principal=profile.service_account_email,
                target_scopes=list(scopes),
                lifetime=self._lifetime_seconds,
            )

        return await asyncio.to_thread(build)

    @staticmethod
    async def access_token(credentials: google_credentials.Credentials) -> str:
        def refresh() -> str:
            if not credentials.valid or credentials.expired:
                credentials.refresh(Request())  # type: ignore[no-untyped-call]
            token = credentials.token
            if not token:
                raise RuntimeError("Google credential did not return an access token")
            return str(token)

        return await asyncio.to_thread(refresh)
