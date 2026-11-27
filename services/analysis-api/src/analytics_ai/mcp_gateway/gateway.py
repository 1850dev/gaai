"""MCP policy gateway; transports accept only server-prepared invocations."""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from typing import Any

from analytics_ai.authorization.service import AuthorizationService
from analytics_ai.bigquery.query_guard import BigQueryGuard
from analytics_ai.compat import StrEnum
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    AnalysisSession,
    BigQuerySource,
    Ga4AuthMode,
    Ga4Source,
    Ga4StreamScope,
)
from analytics_ai.ga4.table_order import TIME_TABLE_FIELDS, is_channel_table_field
from analytics_ai.repositories.protocols import ApplicationRepository


class McpServer(StrEnum):
    GA4 = "ga4"
    BIGQUERY = "bigquery"


@dataclass(frozen=True, slots=True)
class PreparedInvocation:
    """Fully authorized invocation whose resource fields have been injected."""

    server: McpServer
    tool_name: str
    arguments: dict[str, Any]
    user_id: str
    session_id: str
    company_id: str
    data_source_set_id: str
    data_access_profile_id: str | None
    service_account_email: str | None
    ga4_auth_mode: Ga4AuthMode | None = None


class ToolPolicyDenied(ValueError):
    pass


GA4_ANALYSIS_TOOLS = frozenset(
    {
        "get_property_details",
        "get_custom_dimensions_and_metrics",
        "run_report",
        "run_realtime_report",
        "run_funnel_report",
    }
)
BIGQUERY_ANALYSIS_TOOLS = frozenset(
    {
        "get_dataset_info",
        "list_table_ids",
        "get_table_info",
        "execute_sql_readonly",
    }
)
GA4_FILTERABLE_TOOLS = frozenset({"run_report", "run_realtime_report"})
MAX_ANALYSIS_ROWS = 3_000


class McpGateway:
    def __init__(
        self,
        settings: Settings,
        repository: ApplicationRepository,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._authorization = AuthorizationService(repository)
        self._query_guard = BigQueryGuard(settings.bigquery_max_result_rows)

    async def prepare(
        self,
        *,
        user_id: str,
        session_id: str,
        server: McpServer,
        tool_name: str,
        proposed_arguments: dict[str, Any],
        trusted_dry_run: bool = True,
    ) -> PreparedInvocation:
        session = await self._repository.get_session(session_id)
        if session is None:
            raise ToolPolicyDenied("session_not_found")
        scope = await self._authorization.authorize_session(user_id, session)
        execution_profile = None

        if server is McpServer.GA4:
            if tool_name not in GA4_ANALYSIS_TOOLS:
                raise ToolPolicyDenied("ga4_tool_not_allowed")
            ga4_source = scope.source_set.ga4
            if ga4_source is None:
                raise ToolPolicyDenied("ga4_not_registered")
            if ga4_source.auth_mode is Ga4AuthMode.SERVICE_ACCOUNT:
                execution_profile = scope.profile
                if execution_profile is None or not execution_profile.ready:
                    raise ToolPolicyDenied("data_access_profile_not_verified")
                if tool_name not in execution_profile.allowed_mcp_tools:
                    raise ToolPolicyDenied("profile_tool_not_allowed")
            arguments = self._prepare_ga4(
                tool_name,
                proposed_arguments,
                ga4_source,
            )
        else:
            if not self._settings.enable_bigquery_tools:
                raise ToolPolicyDenied("bigquery_tools_disabled")
            if tool_name not in BIGQUERY_ANALYSIS_TOOLS:
                raise ToolPolicyDenied("bigquery_tool_not_allowed")
            bigquery_source = scope.source_set.bigquery
            if bigquery_source is None:
                raise ToolPolicyDenied("bigquery_not_registered")
            execution_profile = scope.profile
            if execution_profile is None or not execution_profile.ready:
                raise ToolPolicyDenied("data_access_profile_not_verified")
            if tool_name not in execution_profile.allowed_mcp_tools:
                raise ToolPolicyDenied("profile_tool_not_allowed")
            arguments = self._prepare_bigquery(
                tool_name,
                proposed_arguments,
                bigquery_source,
                session,
                trusted_dry_run,
            )

        return PreparedInvocation(
            server=server,
            tool_name=tool_name,
            arguments=arguments,
            user_id=user_id,
            session_id=session_id,
            company_id=scope.source_set.company_id,
            data_source_set_id=scope.source_set.id,
            data_access_profile_id=(
                execution_profile.id if execution_profile is not None else None
            ),
            service_account_email=(
                execution_profile.service_account_email
                if execution_profile is not None
                else None
            ),
            ga4_auth_mode=(
                scope.source_set.ga4.auth_mode
                if server is McpServer.GA4 and scope.source_set.ga4 is not None
                else None
            ),
        )

    def _prepare_ga4(
        self,
        tool_name: str,
        proposed: dict[str, Any],
        source: Ga4Source,
    ) -> dict[str, Any]:
        arguments = dict(proposed)
        arguments["property_id"] = source.property_id
        if tool_name == "run_funnel_report":
            return (
                self._prepare_ga4_funnel(arguments, source.stream_scopes)
                if source.stream_scopes
                else arguments
            )
        if tool_name not in GA4_FILTERABLE_TOOLS:
            return arguments

        proposed_limit = arguments.get("limit")
        if proposed_limit is None:
            arguments["limit"] = MAX_ANALYSIS_ROWS
        elif (
            isinstance(proposed_limit, bool)
            or not isinstance(proposed_limit, int)
            or proposed_limit <= 0
        ):
            raise ToolPolicyDenied("ga4_limit_invalid")
        else:
            arguments["limit"] = min(proposed_limit, MAX_ANALYSIS_ROWS)

        ranges = arguments.get("date_ranges")
        if tool_name == "run_report" and isinstance(ranges, list):
            if not 1 <= len(ranges) <= 4:
                raise ToolPolicyDenied("ga4_date_range_count_invalid")
            # Reserve space for every period without expanding the global cap.
            arguments["limit"] = min(
                arguments["limit"], MAX_ANALYSIS_ROWS // len(ranges)
            )

        self._validate_ga4_order_bys(arguments)

        if not source.stream_scopes:
            return arguments

        proposed_filter = arguments.get("dimension_filter")
        if proposed_filter is not None and not isinstance(proposed_filter, dict):
            raise ToolPolicyDenied("ga4_dimension_filter_invalid")
        scope_filter = self._ga4_stream_filter(source.stream_scopes)
        if proposed_filter is None:
            arguments["dimension_filter"] = scope_filter
        else:
            arguments["dimension_filter"] = {
                "and_group": {"expressions": [scope_filter, proposed_filter]}
            }
        return arguments

    @staticmethod
    def _validate_ga4_order_bys(arguments: dict[str, Any]) -> None:
        """Allow selected metrics or reviewed dimensions; never arbitrary fields."""

        proposed = arguments.get("order_bys")
        if proposed is None:
            return
        if not isinstance(proposed, list) or not 1 <= len(proposed) <= 3:
            raise ToolPolicyDenied("ga4_order_by_invalid")
        metrics = arguments.get("metrics")
        allowed_metrics = (
            {item for item in metrics if isinstance(item, str)}
            if isinstance(metrics, list)
            else set()
        )
        dimensions = arguments.get("dimensions")
        allowed_dimensions = (
            {item for item in dimensions if isinstance(item, str)}
            if isinstance(dimensions, list)
            else set()
        )
        for order_by in proposed:
            if not isinstance(order_by, dict) or not isinstance(
                order_by.get("desc"), bool
            ):
                raise ToolPolicyDenied("ga4_order_by_invalid")
            if set(order_by) == {"metric", "desc"}:
                metric = order_by["metric"]
                if not isinstance(metric, dict) or set(metric) != {"metric_name"}:
                    raise ToolPolicyDenied("ga4_order_by_invalid")
                metric_name = metric["metric_name"]
                if (
                    not isinstance(metric_name, str)
                    or metric_name not in allowed_metrics
                ):
                    raise ToolPolicyDenied("ga4_order_by_outside_selected_metrics")
            elif set(order_by) == {"dimension", "desc"}:
                dimension = order_by["dimension"]
                if not isinstance(dimension, dict) or set(dimension) != {
                    "dimension_name"
                }:
                    raise ToolPolicyDenied("ga4_order_by_invalid")
                field = dimension["dimension_name"]
                if (
                    not isinstance(field, str)
                    or field not in allowed_dimensions
                    or not (field in TIME_TABLE_FIELDS or is_channel_table_field(field))
                    or order_by["desc"]
                ):
                    raise ToolPolicyDenied("ga4_order_by_outside_chronological_table")
            else:
                raise ToolPolicyDenied("ga4_order_by_invalid")

    def _prepare_ga4_funnel(
        self,
        arguments: dict[str, Any],
        stream_scopes: tuple[Ga4StreamScope, ...],
    ) -> dict[str, Any]:
        if arguments.get("segments"):
            raise ToolPolicyDenied("ga4_stream_scope_unsupported_for_funnel_segments")
        if arguments.get("funnel_next_action"):
            raise ToolPolicyDenied(
                "ga4_stream_scope_unsupported_for_funnel_next_action"
            )
        proposed_steps = arguments.get("funnel_steps")
        if not isinstance(proposed_steps, list) or not proposed_steps:
            raise ToolPolicyDenied("ga4_funnel_steps_invalid")

        scope_filter = self._ga4_funnel_stream_filter(stream_scopes)
        prepared_steps: list[dict[str, Any]] = []
        for index, step in enumerate(proposed_steps, start=1):
            if not isinstance(step, dict):
                raise ToolPolicyDenied("ga4_funnel_step_invalid")
            name = step.get("name", f"Step {index}")
            if not isinstance(name, str) or not name:
                raise ToolPolicyDenied("ga4_funnel_step_invalid")
            if "filter_expression" in step:
                proposed_filter = step["filter_expression"]
                if not isinstance(proposed_filter, dict):
                    raise ToolPolicyDenied("ga4_funnel_step_filter_invalid")
            else:
                event_name = step.get("event")
                if not isinstance(event_name, str) or not event_name:
                    raise ToolPolicyDenied("ga4_funnel_step_filter_invalid")
                proposed_filter = {"funnel_event_filter": {"event_name": event_name}}
            prepared_steps.append(
                {
                    "name": name,
                    "filter_expression": {
                        "and_group": {"expressions": [scope_filter, proposed_filter]}
                    },
                }
            )
        arguments["funnel_steps"] = prepared_steps
        return arguments

    @classmethod
    def _ga4_stream_filter(
        cls,
        stream_scopes: tuple[Ga4StreamScope, ...],
    ) -> dict[str, Any]:
        expressions = [
            {
                "and_group": {
                    "expressions": [
                        cls._ga4_exact_filter("platform", stream.platform.value),
                        cls._ga4_exact_filter("streamId", stream.stream_id),
                    ]
                }
            }
            for stream in stream_scopes
        ]
        if len(expressions) == 1:
            return expressions[0]
        return {"or_group": {"expressions": expressions}}

    @classmethod
    def _ga4_funnel_stream_filter(
        cls,
        stream_scopes: tuple[Ga4StreamScope, ...],
    ) -> dict[str, Any]:
        expressions = [
            {
                "and_group": {
                    "expressions": [
                        cls._ga4_funnel_exact_filter("platform", stream.platform.value),
                        cls._ga4_funnel_exact_filter("streamId", stream.stream_id),
                    ]
                }
            }
            for stream in stream_scopes
        ]
        if len(expressions) == 1:
            return expressions[0]
        return {"or_group": {"expressions": expressions}}

    @staticmethod
    def _ga4_exact_filter(field_name: str, value: str) -> dict[str, Any]:
        return {
            "filter": {
                "field_name": field_name,
                "string_filter": {
                    "match_type": "EXACT",
                    "value": value,
                    "case_sensitive": False,
                },
            }
        }

    @staticmethod
    def _ga4_funnel_exact_filter(field_name: str, value: str) -> dict[str, Any]:
        return {
            "funnel_field_filter": {
                "field_name": field_name,
                "string_filter": {
                    "match_type": "EXACT",
                    "value": value,
                    "case_sensitive": False,
                },
            }
        }

    def _prepare_bigquery(
        self,
        tool_name: str,
        proposed: dict[str, Any],
        source: BigQuerySource,
        session: AnalysisSession,
        trusted_dry_run: bool,
    ) -> dict[str, Any]:
        if tool_name == "execute_sql_readonly":
            query = proposed.get("query")
            if not isinstance(query, str):
                raise ToolPolicyDenied("query_required")
            validated = self._query_guard.validate(query, source)
            return {
                "projectId": self._settings.platform_project_id,
                "query": validated.sql,
                "dryRun": trusted_dry_run,
                "labels": {
                    "company": self._safe_label(session.company_id),
                    "source_set": self._safe_label(session.data_source_set_id),
                    "session": self._safe_label(session.id),
                },
            }

        arguments = dict(proposed)
        arguments["projectId"] = source.project_id
        requested_dataset = arguments.get("datasetId")
        if requested_dataset not in source.dataset_ids:
            raise ToolPolicyDenied("dataset_outside_allowlist")
        if tool_name == "get_table_info":
            table_id = arguments.get("tableId")
            resource = f"{source.project_id}.{requested_dataset}.{table_id}"
            if not any(
                fnmatch.fnmatchcase(resource, pattern)
                for pattern in source.allowed_resources
            ):
                raise ToolPolicyDenied("table_outside_allowlist")
        return arguments

    @staticmethod
    def _safe_label(value: str) -> str:
        return "x" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
