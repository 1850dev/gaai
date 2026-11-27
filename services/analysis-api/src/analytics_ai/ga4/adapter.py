"""Request-scoped credential adapter for pinned official GA4 MCP tools."""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import importlib.metadata
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from analytics_mcp.tools.utils import proto_to_dict
from google.analytics import data_v1beta
from google.auth import credentials as google_credentials

PINNED_UPSTREAM_VERSION: Final = "0.7.0"
_credential_context: contextvars.ContextVar[google_credentials.Credentials] = (
    contextvars.ContextVar("ga4_mcp_request_credentials")
)


class Ga4AdapterContractError(RuntimeError):
    """Pinned upstream contract changed and must be reviewed."""


def _request_credentials() -> google_credentials.Credentials:
    try:
        return _credential_context.get()
    except LookupError as error:
        raise RuntimeError("GA4 tool called outside a credential scope") from error


def install_request_scoped_provider() -> None:
    version = importlib.metadata.version("analytics-mcp")
    if version != PINNED_UPSTREAM_VERSION:
        raise Ga4AdapterContractError(
            f"expected analytics-mcp {PINNED_UPSTREAM_VERSION}, got {version}"
        )
    client_module = importlib.import_module("analytics_mcp.tools.client")
    if not hasattr(client_module, "_get_credentials"):
        raise Ga4AdapterContractError("upstream credential hook is missing")
    client_module.__dict__["_get_credentials"] = _request_credentials


def _load_tools() -> dict[str, Callable[..., Awaitable[dict[str, Any]]]]:
    modules = {
        "get_account_summaries": (
            "analytics_mcp.tools.admin.info",
            "get_account_summaries",
        ),
        "get_property_details": (
            "analytics_mcp.tools.admin.info",
            "get_property_details",
        ),
        "list_google_ads_links": (
            "analytics_mcp.tools.admin.info",
            "list_google_ads_links",
        ),
        "get_custom_dimensions_and_metrics": (
            "analytics_mcp.tools.reporting.metadata",
            "get_custom_dimensions_and_metrics",
        ),
        "run_report": ("analytics_mcp.tools.reporting.core", "run_report"),
        "run_realtime_report": (
            "analytics_mcp.tools.reporting.realtime",
            "run_realtime_report",
        ),
        "run_funnel_report": (
            "analytics_mcp.tools.reporting.funnel",
            "run_funnel_report",
        ),
    }
    result: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}
    for tool_name, (module_name, function_name) in modules.items():
        module = importlib.import_module(module_name)
        function = getattr(module, function_name, None)
        if function is None or not callable(function):
            raise Ga4AdapterContractError(f"upstream tool missing: {tool_name}")
        result[tool_name] = function
    return result


class Ga4McpAdapter:
    """Call official tool functions inside a request credential context."""

    def __init__(
        self,
        tools: Mapping[str, Callable[..., Awaitable[dict[str, Any]]]] | None = None,
    ) -> None:
        if tools is None:
            install_request_scoped_provider()
            tools = _load_tools()
        self._tools = dict(tools)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._tools)

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> dict[str, Any]:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ValueError("GA4 tool is not registered")
        token = _credential_context.set(credentials)
        try:
            return await tool(**dict(arguments))
        finally:
            _credential_context.reset(token)

    async def check_compatibility(
        self,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> dict[str, Any]:
        """Check a Core report with the same request-scoped OAuth credential."""

        property_id = arguments.get("property_id")
        dimensions = arguments.get("dimensions", [])
        metrics = arguments.get("metrics", [])
        if not isinstance(property_id, (str, int)):
            raise ValueError("GA4 property ID is required")
        if not isinstance(dimensions, list) or not all(
            isinstance(item, str) for item in dimensions
        ):
            raise ValueError("GA4 dimensions are invalid")
        if not isinstance(metrics, list) or not all(
            isinstance(item, str) for item in metrics
        ):
            raise ValueError("GA4 metrics are invalid")
        request = data_v1beta.CheckCompatibilityRequest(
            property=f"properties/{str(property_id).removeprefix('properties/')}",
            dimensions=[data_v1beta.Dimension(name=item) for item in dimensions],
            metrics=[data_v1beta.Metric(name=item) for item in metrics],
        )
        dimension_filter = arguments.get("dimension_filter")
        metric_filter = arguments.get("metric_filter")
        if dimension_filter:
            request.dimension_filter = data_v1beta.FilterExpression(dimension_filter)
        if metric_filter:
            request.metric_filter = data_v1beta.FilterExpression(metric_filter)

        def check() -> dict[str, Any]:
            client = data_v1beta.BetaAnalyticsDataClient(credentials=credentials)
            try:
                response = proto_to_dict(client.check_compatibility(request=request))
                return dict(response)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        return await asyncio.to_thread(check)
