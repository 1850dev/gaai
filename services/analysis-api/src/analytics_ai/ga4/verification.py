"""Verify the exact registered GA4 Property using the Set's explicit auth mode."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from google.api_core import exceptions as google_exceptions
from pydantic import Field

from analytics_ai.authorization.service import AuthorizationService
from analytics_ai.domain.models import (
    AuditEvent,
    DataAccessProfile,
    DataSourceSet,
    FrozenModel,
    Ga4PropertyAccessRecord,
    Ga4PropertyAccessStatus,
)
from analytics_ai.ga4.credentials import (
    Ga4CredentialError,
    Ga4ExecutionCredentialProvider,
)
from analytics_ai.ga4.discovery import (
    Ga4DiscoveryClient,
    Ga4PropertyType,
)
from analytics_ai.repositories.protocols import ApplicationRepository

VERIFICATION_TOOL = "get_property_details"
RESOURCE_NAME = re.compile(r"^(accounts|properties)/([0-9]+)$")
SERVICE_LEVELS = frozenset({"GOOGLE_ANALYTICS_STANDARD", "GOOGLE_ANALYTICS_360"})


class Ga4PropertyVerification(FrozenModel):
    data_source_set_id: str
    property_id: str = Field(pattern=r"^[0-9]+$")
    display_name: str = Field(min_length=1, max_length=200)
    property_type: Ga4PropertyType
    parent: str = Field(pattern=r"^(accounts|properties)/[0-9]+$")
    time_zone: str = Field(min_length=1, max_length=100)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    service_level: str = Field(min_length=1, max_length=100)
    stream_scope_verification: str = "pending_report_acceptance"


class Ga4VerificationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Ga4PropertyVerificationService:
    """Verify access without accepting a Property ID from the HTTP caller."""

    def __init__(
        self,
        repository: ApplicationRepository,
        *,
        credentials: Ga4ExecutionCredentialProvider,
        ga4_client: Ga4DiscoveryClient,
    ) -> None:
        self._repository = repository
        self._authorization = AuthorizationService(repository)
        self._credentials = credentials
        self._ga4 = ga4_client

    async def verify(
        self,
        *,
        user_id: str,
        data_source_set_id: str,
        request_id: str,
    ) -> Ga4PropertyVerification:
        source_set = await self._repository.get_data_source_set(data_source_set_id)
        if source_set is None or source_set.ga4 is None:
            raise Ga4VerificationError("ga4_data_source_set_not_found")
        scope = await self._authorization.authorize_source_set(
            user_id, data_source_set_id
        )
        profile = scope.profile
        property_id = source_set.ga4.property_id
        try:
            credential = await self._credentials.get_credentials(
                user_id=user_id,
                source_set=source_set,
                profile=profile,
            )
            raw = await self._ga4.call(
                VERIFICATION_TOOL,
                {"property_id": property_id},
                credential.credentials,
            )
            result = self._normalize(raw, source_set)
            await self._repository.put_ga4_property_access(
                Ga4PropertyAccessRecord(
                    user_id=user_id,
                    property_id=property_id,
                    status=Ga4PropertyAccessStatus.ACCESSIBLE,
                )
            )
        except google_exceptions.Forbidden as error:
            await self._repository.put_ga4_property_access(
                Ga4PropertyAccessRecord(
                    user_id=user_id,
                    property_id=property_id,
                    status=Ga4PropertyAccessStatus.DENIED,
                )
            )
            denied = Ga4VerificationError("ga4_property_access_denied")
            await self._audit(
                source_set=source_set,
                profile=profile,
                user_id=user_id,
                request_id=request_id,
                status="error",
                details={"error_code": denied.code},
            )
            raise denied from error
        except Ga4CredentialError as error:
            denied = Ga4VerificationError(error.code)
            await self._audit(
                source_set=source_set,
                profile=profile,
                user_id=user_id,
                request_id=request_id,
                status="error",
                details={"error_code": denied.code},
            )
            raise denied from error
        except Ga4VerificationError as error:
            await self._audit(
                source_set=source_set,
                profile=profile,
                user_id=user_id,
                request_id=request_id,
                status="error",
                details={"error_code": error.code},
            )
            raise
        except Exception as error:
            await self._audit(
                source_set=source_set,
                profile=profile,
                user_id=user_id,
                request_id=request_id,
                status="error",
                details={"error_code": type(error).__name__},
            )
            raise Ga4VerificationError("ga4_property_verification_failed") from error

        await self._audit(
            source_set=source_set,
            profile=profile,
            user_id=user_id,
            request_id=request_id,
            status="success",
            details={
                "property_type": result.property_type.value,
                "service_level": result.service_level,
            },
        )
        return result

    @classmethod
    def _normalize(
        cls,
        raw: object,
        source_set: DataSourceSet,
    ) -> Ga4PropertyVerification:
        ga4 = source_set.ga4
        if ga4 is None or not isinstance(raw, Mapping):
            raise Ga4VerificationError("ga4_property_details_contract_invalid")
        expected_name = f"properties/{ga4.property_id}"
        if raw.get("name") != expected_name:
            raise Ga4VerificationError("ga4_registered_property_mismatch")

        time_zone = cls._required_text(raw.get("time_zone"), max_length=100)
        currency_code = cls._required_text(raw.get("currency_code"), max_length=3)
        if time_zone != ga4.timezone:
            raise Ga4VerificationError("ga4_registered_timezone_mismatch")
        if currency_code != ga4.currency_code:
            raise Ga4VerificationError("ga4_registered_currency_mismatch")

        service_level = cls._required_text(raw.get("service_level"), max_length=100)
        if service_level not in SERVICE_LEVELS:
            raise Ga4VerificationError("ga4_property_details_contract_invalid")
        try:
            property_type = Ga4PropertyType(
                cls._required_text(raw.get("property_type"), max_length=100)
            )
            return Ga4PropertyVerification(
                data_source_set_id=source_set.id,
                property_id=ga4.property_id,
                display_name=cls._required_text(raw.get("display_name")),
                property_type=property_type,
                parent=cls._resource_name(raw.get("parent")),
                time_zone=time_zone,
                currency_code=currency_code,
                service_level=service_level,
            )
        except (TypeError, ValueError) as error:
            raise Ga4VerificationError(
                "ga4_property_details_contract_invalid"
            ) from error

    @staticmethod
    def _required_text(value: object, *, max_length: int = 200) -> str:
        if not isinstance(value, str) or not value or len(value) > max_length:
            raise Ga4VerificationError("ga4_property_details_contract_invalid")
        return value

    @staticmethod
    def _resource_name(value: object) -> str:
        if not isinstance(value, str) or RESOURCE_NAME.fullmatch(value) is None:
            raise Ga4VerificationError("ga4_property_details_contract_invalid")
        return value

    async def _audit(
        self,
        *,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
        user_id: str,
        request_id: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        ga4 = source_set.ga4
        property_id = ga4.property_id if ga4 is not None else "unavailable"
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action="ga4.property.verify",
                user_id=user_id,
                company_id=source_set.company_id,
                data_source_set_id=source_set.id,
                data_access_profile_id=profile.id if profile is not None else None,
                status=status,
                details={
                    "mcp_server": "ga4",
                    "mcp_tool": VERIFICATION_TOOL,
                    "ga4_property_id": property_id,
                    "ga4_auth_mode": (
                        source_set.ga4.auth_mode.value
                        if source_set.ga4 is not None
                        else "unavailable"
                    ),
                    **details,
                },
            )
        )
