"""GA4 Property discovery through the signed-in user's connected OAuth identity."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from google.auth import credentials as google_credentials
from pydantic import Field

from analytics_ai.authorization.service import AccessDenied
from analytics_ai.compat import StrEnum
from analytics_ai.domain.models import (
    AuditEvent,
    FrozenModel,
    Status,
)
from analytics_ai.ga4.credentials import Ga4CredentialContext, Ga4CredentialError
from analytics_ai.repositories.protocols import ApplicationRepository

DISCOVERY_TOOL = "get_account_summaries"
MAX_DISCOVERED_PROPERTIES = 1_000
RESOURCE_NAME = re.compile(r"^(accounts|properties)/([0-9]+)$")


class Ga4UserCredentialProvider(Protocol):
    async def get_user_credentials(self, *, user_id: str) -> Ga4CredentialContext: ...


class Ga4DiscoveryClient(Protocol):
    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> object: ...


class Ga4PropertyType(StrEnum):
    ORDINARY = "PROPERTY_TYPE_ORDINARY"
    SUBPROPERTY = "PROPERTY_TYPE_SUBPROPERTY"
    ROLLUP = "PROPERTY_TYPE_ROLLUP"


class Ga4PropertyCandidate(FrozenModel):
    account_id: str = Field(pattern=r"^[0-9]+$")
    account_display_name: str = Field(min_length=1, max_length=200)
    property_id: str = Field(pattern=r"^[0-9]+$")
    property_display_name: str = Field(min_length=1, max_length=200)
    property_type: Ga4PropertyType
    parent: str = Field(pattern=r"^(accounts|properties)/[0-9]+$")
    can_edit: bool


class Ga4DiscoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Ga4PropertyDiscoveryService:
    """List only Properties visible to the requesting user's Google account."""

    def __init__(
        self,
        repository: ApplicationRepository,
        *,
        credentials: Ga4UserCredentialProvider,
        ga4_client: Ga4DiscoveryClient,
    ) -> None:
        self._repository = repository
        self._credentials = credentials
        self._ga4 = ga4_client

    async def discover(
        self,
        *,
        user_id: str,
        request_id: str,
    ) -> list[Ga4PropertyCandidate]:
        user = await self._repository.get_user(user_id)
        if user is None or user.status is not Status.ACTIVE:
            await self._audit(
                user_id=user_id,
                request_id=request_id,
                status="denied",
                details={"error_code": "inactive_or_unregistered_user"},
            )
            raise AccessDenied("inactive_or_unregistered_user")

        try:
            context = await self._credentials.get_user_credentials(user_id=user_id)
            raw = await self._ga4.call(DISCOVERY_TOOL, {}, context.credentials)
            candidates = self._normalize(raw)
        except (Ga4DiscoveryError, Ga4CredentialError) as error:
            await self._audit(
                user_id=user_id,
                request_id=request_id,
                status="error",
                details={"error_code": error.code},
            )
            raise Ga4DiscoveryError(error.code) from error
        except Exception as error:
            await self._audit(
                user_id=user_id,
                request_id=request_id,
                status="error",
                details={"error_code": type(error).__name__},
            )
            raise Ga4DiscoveryError("ga4_property_discovery_failed") from error

        await self._audit(
            user_id=user_id,
            request_id=request_id,
            status="success",
            details={
                "account_count": len({item.account_id for item in candidates}),
                "candidate_count": len(candidates),
            },
        )
        return candidates

    @classmethod
    def _normalize(cls, raw: object) -> list[Ga4PropertyCandidate]:
        if not isinstance(raw, list):
            raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")

        candidates: list[Ga4PropertyCandidate] = []
        property_ids: set[str] = set()
        for account in raw:
            if not isinstance(account, Mapping):
                raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")
            account_id = cls._resource_id(account.get("account"), "accounts")
            account_name = cls._required_text(account.get("display_name"))
            summaries = account.get("property_summaries", [])
            if not isinstance(summaries, list):
                raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")
            for summary in summaries:
                if not isinstance(summary, Mapping):
                    raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")
                property_id = cls._resource_id(summary.get("property"), "properties")
                if property_id in property_ids:
                    raise Ga4DiscoveryError("ga4_duplicate_property_summary")
                property_ids.add(property_id)
                try:
                    property_type = Ga4PropertyType(
                        cls._required_text(summary.get("property_type"))
                    )
                    candidate = Ga4PropertyCandidate(
                        account_id=account_id,
                        account_display_name=account_name,
                        property_id=property_id,
                        property_display_name=cls._required_text(
                            summary.get("display_name")
                        ),
                        property_type=property_type,
                        parent=cls._resource_name(summary.get("parent")),
                        can_edit=summary.get("can_edit") is True,
                    )
                except (TypeError, ValueError) as error:
                    raise Ga4DiscoveryError(
                        "ga4_account_summaries_contract_invalid"
                    ) from error
                candidates.append(candidate)
                if len(candidates) > MAX_DISCOVERED_PROPERTIES:
                    raise Ga4DiscoveryError("ga4_property_discovery_limit_exceeded")

        return sorted(
            candidates,
            key=lambda item: (
                item.account_display_name.casefold(),
                item.property_display_name.casefold(),
                item.property_id,
            ),
        )

    @staticmethod
    def _required_text(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 200:
            raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")
        return value

    @staticmethod
    def _resource_name(value: object) -> str:
        if not isinstance(value, str) or RESOURCE_NAME.fullmatch(value) is None:
            raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")
        return value

    @classmethod
    def _resource_id(cls, value: object, resource_type: str) -> str:
        resource_name = cls._resource_name(value)
        match = RESOURCE_NAME.fullmatch(resource_name)
        if match is None or match.group(1) != resource_type:
            raise Ga4DiscoveryError("ga4_account_summaries_contract_invalid")
        return match.group(2)

    async def _audit(
        self,
        *,
        user_id: str,
        request_id: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action="ga4.properties.discover",
                user_id=user_id,
                status=status,
                details={
                    "mcp_server": "ga4",
                    "mcp_tool": DISCOVERY_TOOL,
                    "ga4_auth_mode": "USER_OAUTH",
                    **details,
                },
            )
        )
