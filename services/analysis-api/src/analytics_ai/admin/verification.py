"""Bounded read-only metadata verification with request-scoped credentials."""

import asyncio
import re
from typing import Protocol

from google.analytics import admin_v1beta
from google.auth import credentials as google_credentials

from analytics_ai.admin.models import RegistrationError, VerificationResult
from analytics_ai.domain.models import DataAccessProfile, DataSourceSet, Ga4Platform
from analytics_ai.ga4.adapter import Ga4McpAdapter
from analytics_ai.ga4.credentials import Ga4ExecutionCredentialProvider
from analytics_ai.ga4.discovery import Ga4DiscoveryClient, Ga4PropertyType
from analytics_ai.ga4.verification import (
    Ga4PropertyVerification,
    Ga4PropertyVerificationService,
)


class RegistrationVerifier(Protocol):
    async def verify(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> str | VerificationResult: ...


class StreamReader(Protocol):
    async def read(
        self,
        source: DataSourceSet,
        credentials: google_credentials.Credentials,
        verification: Ga4PropertyVerification | None = None,
    ) -> dict[str, str]: ...


class GoogleStreamReader:
    async def read(
        self,
        source: DataSourceSet,
        credentials: google_credentials.Credentials,
        verification: Ga4PropertyVerification | None = None,
    ) -> dict[str, str]:
        def fetch() -> dict[str, str]:
            assert source.ga4 is not None
            parent = f"properties/{source.ga4.property_id}"
            if verification is not None:
                if verification.property_id != source.ga4.property_id:
                    raise RegistrationError("registration_stream_mismatch", 422)
                if verification.property_type is Ga4PropertyType.SUBPROPERTY:
                    if (
                        not re.fullmatch(r"properties/[0-9]+", verification.parent)
                        or verification.parent == parent
                    ):
                        raise RegistrationError(
                            "registration_stream_parent_invalid", 422
                        )
                    # Inherited stream metadata is owned by the confirmed source
                    # property. This never changes the analytical query Property.
                    parent = verification.parent
            result: dict[str, str] = {}
            # This is a management-only metadata adapter, not an agent tool.
            with admin_v1beta.AnalyticsAdminServiceClient(
                credentials=credentials
            ) as client:
                for stream in source.ga4.stream_scopes:
                    name = parent + f"/dataStreams/{stream.stream_id}"
                    response = client.get_data_stream(name=name, retry=None, timeout=5)
                    if response.name != name:
                        raise RegistrationError("registration_stream_mismatch", 422)
                    logical_name = (
                        f"properties/{source.ga4.property_id}"
                        f"/dataStreams/{stream.stream_id}"
                    )
                    result[logical_name] = admin_v1beta.DataStream.DataStreamType(
                        response.type_
                    ).name
            return result

        return await asyncio.to_thread(fetch)


class Ga4RegistrationVerifier:
    def __init__(
        self,
        credentials: Ga4ExecutionCredentialProvider,
        *,
        client: Ga4DiscoveryClient | None = None,
        streams: StreamReader | None = None,
    ) -> None:
        self.credentials = credentials
        self.client = client or Ga4McpAdapter()
        self.streams = streams or GoogleStreamReader()

    async def verify(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> VerificationResult:
        assert source_set.ga4 is not None
        credential = await self.credentials.get_credentials(
            user_id=user_id, source_set=source_set, profile=profile
        )
        if credential.auth_mode != source_set.ga4.auth_mode:
            raise RegistrationError("registration_credential_mode_mismatch", 403)
        raw = await self.client.call(
            "get_property_details",
            {"property_id": source_set.ga4.property_id},
            credential.credentials,
        )
        verified = Ga4PropertyVerificationService._normalize(raw, source_set)
        streams = await self.streams.read(source_set, credential.credentials, verified)
        types = {
            Ga4Platform.WEB: "WEB_DATA_STREAM",
            Ga4Platform.IOS: "IOS_APP_DATA_STREAM",
            Ga4Platform.ANDROID: "ANDROID_APP_DATA_STREAM",
        }
        for stream in source_set.ga4.stream_scopes:
            name = (
                f"properties/{source_set.ga4.property_id}"
                f"/dataStreams/{stream.stream_id}"
            )
            if streams.get(name) != types[stream.platform]:
                raise RegistrationError("registration_stream_mismatch", 422)
        identity = credential.google_subject or credential.service_account_email
        if not identity:
            raise RegistrationError("registration_credential_identity_missing", 403)
        return VerificationResult(
            credential_identity=identity,
            stream_metadata_parent=verified.parent
            if verified.property_type is Ga4PropertyType.SUBPROPERTY
            else f"properties/{source_set.ga4.property_id}",
        )
