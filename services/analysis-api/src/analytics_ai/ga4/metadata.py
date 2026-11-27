"""Authorized discovery of Property-specific GA4 custom definitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import Field

from analytics_ai.domain.models import FrozenModel
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import McpServer, PreparedInvocation

MAX_CUSTOM_DEFINITIONS = 500


class Ga4CustomDefinition(FrozenModel):
    """Review candidate only; this model never activates a Semantic definition."""

    api_name: str = Field(min_length=1, max_length=300)
    ui_name: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2_000)
    category: str = Field(default="", max_length=300)
    definition_type: str = Field(pattern=r"^(dimension|metric)$")


class Ga4MetadataCatalog(FrozenModel):
    custom_dimensions: tuple[Ga4CustomDefinition, ...] = ()
    custom_metrics: tuple[Ga4CustomDefinition, ...] = ()


class MetadataGateway(Protocol):
    async def prepare(
        self,
        *,
        user_id: str,
        session_id: str,
        server: McpServer,
        tool_name: str,
        proposed_arguments: dict[str, Any],
        trusted_dry_run: bool = True,
    ) -> PreparedInvocation: ...


class MetadataExecutor(Protocol):
    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult: ...


class Ga4MetadataCatalogService:
    """Read custom metadata through the same Session, Grant and OAuth boundary."""

    def __init__(self, gateway: MetadataGateway, executor: MetadataExecutor) -> None:
        self._gateway = gateway
        self._executor = executor

    async def inspect(self, *, user_id: str, session_id: str) -> Ga4MetadataCatalog:
        invocation = await self._gateway.prepare(
            user_id=user_id,
            session_id=session_id,
            server=McpServer.GA4,
            tool_name="get_custom_dimensions_and_metrics",
            proposed_arguments={},
        )
        result = await self._executor.execute(invocation)
        return self._normalize(result.payload)

    @classmethod
    def _normalize(cls, payload: Mapping[str, Any]) -> Ga4MetadataCatalog:
        return Ga4MetadataCatalog(
            custom_dimensions=cls._definitions(
                payload, "custom_dimensions", "dimension"
            ),
            custom_metrics=cls._definitions(payload, "custom_metrics", "metric"),
        )

    @staticmethod
    def _definitions(
        payload: Mapping[str, Any],
        key: str,
        definition_type: str,
    ) -> tuple[Ga4CustomDefinition, ...]:
        raw = payload.get(key)
        if not isinstance(raw, list) or len(raw) > MAX_CUSTOM_DEFINITIONS:
            raise ValueError("ga4_custom_metadata_contract_invalid")
        definitions: list[Ga4CustomDefinition] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("ga4_custom_metadata_contract_invalid")
            api_name = item.get("api_name", item.get("apiName"))
            ui_name = item.get("ui_name", item.get("uiName"))
            if not isinstance(api_name, str) or not isinstance(ui_name, str):
                raise ValueError("ga4_custom_metadata_contract_invalid")
            if api_name in seen:
                raise ValueError("ga4_custom_metadata_contract_invalid")
            seen.add(api_name)
            description = item.get("description", "")
            category = item.get("category", "")
            definitions.append(
                Ga4CustomDefinition(
                    api_name=api_name,
                    ui_name=ui_name,
                    description=description if isinstance(description, str) else "",
                    category=category if isinstance(category, str) else "",
                    definition_type=definition_type,
                )
            )
        return tuple(sorted(definitions, key=lambda item: item.api_name))
