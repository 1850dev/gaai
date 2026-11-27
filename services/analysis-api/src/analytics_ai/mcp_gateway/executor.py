"""Credential-selecting MCP executor with BigQuery dry-run cost enforcement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.api_core import exceptions as google_exceptions
from google.auth import credentials as google_credentials

from analytics_ai.bigquery.remote_mcp import BigQueryRemoteMcpClient
from analytics_ai.config import Settings
from analytics_ai.credentials.broker import CredentialBroker
from analytics_ai.credentials.token_cipher import KmsTokenCipher
from analytics_ai.domain.models import (
    AuditEvent,
    DataAccessProfile,
    DataSourceSet,
    Ga4AuthMode,
    Ga4PropertyAccessRecord,
    Ga4PropertyAccessStatus,
    ReportQuality,
)
from analytics_ai.ga4.adapter import Ga4McpAdapter
from analytics_ai.ga4.credentials import (
    Ga4CredentialContext,
    Ga4CredentialError,
    Ga4CredentialProvider,
    Ga4ExecutionCredentialProvider,
)
from analytics_ai.mcp_gateway.gateway import (
    MAX_ANALYSIS_ROWS,
    McpServer,
    PreparedInvocation,
)
from analytics_ai.observability.logging import request_id_context
from analytics_ai.repositories.protocols import ApplicationRepository


class BigQueryCredentialProvider(Protocol):
    async def for_ga4(
        self, profile: DataAccessProfile
    ) -> google_credentials.Credentials: ...

    async def for_bigquery(
        self, profile: DataAccessProfile
    ) -> google_credentials.Credentials: ...


class Ga4ToolClient(Protocol):
    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> dict[str, Any]: ...

    async def check_compatibility(
        self,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> dict[str, Any]: ...


class BigQueryToolClient(Protocol):
    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> dict[str, Any]: ...


class McpExecutionDenied(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        returned_row_count: int | None = None,
        result_row_limit: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.returned_row_count = returned_row_count
        self.result_row_limit = result_row_limit


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    payload: dict[str, Any]
    query_id: str | None
    bytes_processed: int | None
    row_count: int
    attempts: int = 1
    compatibility_checked: bool = False
    quality: ReportQuality = field(default_factory=ReportQuality)
    property_quota: dict[str, int] | None = None
    result_row_limit: int | None = None


class McpExecutionService:
    """Execute only a PreparedInvocation and never accept caller credentials."""

    def __init__(
        self,
        settings: Settings,
        repository: ApplicationRepository,
        *,
        credentials: BigQueryCredentialProvider | None = None,
        ga4_credentials: Ga4ExecutionCredentialProvider | None = None,
        ga4_client: Ga4ToolClient | None = None,
        bigquery_client: BigQueryToolClient | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        service_accounts = credentials or CredentialBroker()
        cipher = (
            KmsTokenCipher(settings.ga4_oauth_kms_key_name)
            if settings.enable_ga4_user_oauth
            else None
        )
        self._bigquery_credentials = service_accounts
        self._ga4_credentials = ga4_credentials or Ga4CredentialProvider(
            settings,
            repository,
            cipher,
            service_accounts=service_accounts,
        )
        self._ga4 = ga4_client or Ga4McpAdapter()
        self._bigquery = bigquery_client or BigQueryRemoteMcpClient(
            settings.bigquery_mcp_endpoint,
            timeout_seconds=settings.external_request_timeout_seconds,
        )

    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult:
        source_set = await self._repository.get_data_source_set(
            invocation.data_source_set_id
        )
        if source_set is None or source_set.company_id != invocation.company_id:
            raise McpExecutionDenied("prepared_source_set_is_not_current")
        profile: DataAccessProfile | None = None
        if invocation.data_access_profile_id is not None:
            profile = await self._repository.get_data_access_profile(
                invocation.data_access_profile_id
            )
            if (
                profile is None
                or not profile.ready
                or profile.service_account_email != invocation.service_account_email
                or profile.company_id != invocation.company_id
            ):
                raise McpExecutionDenied("prepared_profile_is_not_current")
        elif invocation.service_account_email is not None:
            raise McpExecutionDenied("prepared_profile_is_not_current")

        ga4_credential_context: Ga4CredentialContext | None = None
        try:
            async with asyncio.timeout(self._settings.external_request_timeout_seconds):
                if invocation.server is McpServer.GA4:
                    ga4_credential_context = await self._resolve_ga4_credentials(
                        invocation,
                        source_set,
                        profile,
                    )
                    result = await self._execute_ga4(
                        invocation,
                        ga4_credential_context,
                    )
                else:
                    if not self._settings.enable_bigquery_tools:
                        raise McpExecutionDenied("bigquery_tools_disabled")
                    if profile is None:
                        raise McpExecutionDenied("bigquery_profile_required")
                    result = await self._execute_bigquery(invocation, profile)
        except TimeoutError as error:
            denied = McpExecutionDenied("mcp_execution_timeout")
            await self._audit(
                invocation,
                "error",
                error_code=denied.code,
                ga4_credential_context=ga4_credential_context,
            )
            raise denied from error
        except Exception as error:
            error_code = (
                error.code
                if isinstance(error, McpExecutionDenied)
                else type(error).__name__
            )
            await self._audit(
                invocation,
                "error",
                error_code=error_code,
                row_count=(
                    error.returned_row_count
                    if isinstance(error, McpExecutionDenied)
                    else None
                ),
                result_row_limit=(
                    error.result_row_limit
                    if isinstance(error, McpExecutionDenied)
                    else None
                ),
                ga4_credential_context=ga4_credential_context,
            )
            raise

        await self._audit(
            invocation,
            "success",
            query_id=result.query_id,
            bytes_processed=result.bytes_processed,
            row_count=result.row_count,
            result_row_limit=result.result_row_limit,
            attempt_count=result.attempts,
            compatibility_checked=result.compatibility_checked,
            quality=result.quality,
            property_quota=result.property_quota,
            ga4_credential_context=ga4_credential_context,
        )
        return result

    async def _resolve_ga4_credentials(
        self,
        invocation: PreparedInvocation,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext:
        source = source_set.ga4
        if (
            source is None
            or invocation.ga4_auth_mode is None
            or invocation.ga4_auth_mode is not source.auth_mode
        ):
            raise McpExecutionDenied("prepared_ga4_auth_mode_is_not_current")
        try:
            return await self._ga4_credentials.get_credentials(
                user_id=invocation.user_id,
                source_set=source_set,
                profile=profile,
            )
        except Ga4CredentialError as error:
            raise McpExecutionDenied(error.code) from error

    async def _execute_ga4(
        self,
        invocation: PreparedInvocation,
        credential_context: Ga4CredentialContext,
    ) -> ToolExecutionResult:
        compatibility_checked = False
        if invocation.tool_name == "run_report":
            checker = getattr(self._ga4, "check_compatibility", None)
            if checker is not None:
                compatibility = await checker(
                    invocation.arguments,
                    credential_context.credentials,
                )
                self._require_compatible(compatibility, invocation.arguments)
                compatibility_checked = True
        attempts = 0
        while True:
            attempts += 1
            try:
                payload = await self._ga4.call(
                    invocation.tool_name,
                    invocation.arguments,
                    credential_context.credentials,
                )
                break
            except Exception as error:
                if isinstance(error, google_exceptions.Forbidden):
                    await self._record_ga4_property_access(
                        invocation,
                        Ga4PropertyAccessStatus.DENIED,
                    )
                    raise McpExecutionDenied("ga4_property_access_denied") from error
                if (
                    attempts >= self._settings.ga4_max_attempts
                    or not self._is_transient_ga4_error(error)
                ):
                    raise
                await self._audit(
                    invocation,
                    "retry",
                    error_code=type(error).__name__,
                    attempt_count=attempts,
                    ga4_credential_context=credential_context,
                )
                delay = self._settings.ga4_retry_base_seconds * (2 ** (attempts - 1))
                jitter = 0.5 + (secrets.randbelow(1_001) / 1_000)
                await asyncio.sleep(delay * jitter)
        await self._record_ga4_property_access(
            invocation,
            Ga4PropertyAccessStatus.ACCESSIBLE,
        )
        row_count = self._ga4_returned_row_count(payload)
        requested_limit = invocation.arguments.get("limit")
        result_limit = (
            min(requested_limit, MAX_ANALYSIS_ROWS)
            if isinstance(requested_limit, int)
            and not isinstance(requested_limit, bool)
            and requested_limit > 0
            else MAX_ANALYSIS_ROWS
        )
        # Multi-period Core responses can contain one row per date range for
        # each requested position (observed with pinned official MCP 0.7.0).
        # Only accept that expansion when the response proves the partition;
        # never multiply the limit for unlabelled or arbitrary extra rows.
        if invocation.tool_name == "run_report" and row_count > result_limit:
            result_limit = self._partitioned_result_limit(
                payload, invocation.arguments, result_limit
            )
        if row_count > result_limit:
            raise McpExecutionDenied(
                "mcp_result_row_limit_exceeded",
                returned_row_count=row_count,
                result_row_limit=result_limit,
            )
        return ToolExecutionResult(
            payload=payload,
            query_id=None,
            bytes_processed=None,
            row_count=row_count,
            attempts=attempts,
            compatibility_checked=compatibility_checked,
            quality=self._report_quality(payload, invocation.arguments),
            property_quota=self._property_quota(payload),
            result_row_limit=result_limit,
        )

    @staticmethod
    def _partitioned_result_limit(
        payload: Mapping[str, Any], arguments: Mapping[str, Any], limit: int
    ) -> int:
        ranges = arguments.get("date_ranges")
        headers = payload.get("dimension_headers", payload.get("dimensionHeaders"))
        rows = payload.get("rows")
        if (
            not isinstance(ranges, list)
            or not 2 <= len(ranges) <= 4
            or not all(isinstance(r, dict) for r in ranges)
            or not isinstance(headers, list)
            or not isinstance(rows, list)
        ):
            return limit
        names = [r.get("name") or f"date_range_{i}" for i, r in enumerate(ranges)]
        if not all(isinstance(name, str) for name in names) or len(set(names)) != len(
            names
        ):
            return limit
        indexes = [
            i
            for i, header in enumerate(headers)
            if isinstance(header, Mapping) and header.get("name") == "dateRange"
        ]
        if len(indexes) != 1:
            return limit
        counts = dict.fromkeys(names, 0)
        for row in rows:
            if not isinstance(row, Mapping):
                return limit
            values = row.get("dimension_values", row.get("dimensionValues"))
            if not isinstance(values, list) or len(values) != len(headers):
                return limit
            cell = values[indexes[0]]
            name = cell.get("value") if isinstance(cell, Mapping) else None
            if not isinstance(name, str) or name not in counts:
                return limit
            counts[name] += 1
            if counts[name] > limit:
                return limit
        return min(limit * len(ranges), MAX_ANALYSIS_ROWS)

    @staticmethod
    def _require_compatible(
        payload: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> None:
        collections = (
            (
                "dimension_compatibilities",
                "dimensionCompatibilities",
                "dimensions",
                "dimension_metadata",
                "dimensionMetadata",
            ),
            (
                "metric_compatibilities",
                "metricCompatibilities",
                "metrics",
                "metric_metadata",
                "metricMetadata",
            ),
        )
        for (
            snake_key,
            camel_key,
            argument_key,
            metadata_key,
            metadata_camel_key,
        ) in collections:
            requested = arguments.get(argument_key, [])
            if not requested:
                continue
            values = payload.get(snake_key, payload.get(camel_key))
            if not isinstance(values, list):
                raise McpExecutionDenied("ga4_compatibility_response_invalid")
            compatibilities: dict[str, str] = {}
            for item in values:
                if not isinstance(item, Mapping):
                    raise McpExecutionDenied("ga4_compatibility_response_invalid")
                metadata = item.get(metadata_key, item.get(metadata_camel_key))
                if not isinstance(metadata, Mapping):
                    continue
                api_name = metadata.get("api_name", metadata.get("apiName"))
                if isinstance(api_name, str):
                    compatibilities[api_name] = str(
                        item.get("compatibility", "")
                    ).upper()
            for api_name in requested:
                if not isinstance(api_name, str) or api_name not in compatibilities:
                    raise McpExecutionDenied("ga4_compatibility_response_invalid")
                if compatibilities[api_name] != "COMPATIBLE":
                    raise McpExecutionDenied("ga4_metric_dimension_incompatible")

    @staticmethod
    def _report_quality(
        payload: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> ReportQuality:
        metadata: Mapping[str, Any] = {}
        candidate = payload.get("metadata")
        if isinstance(candidate, Mapping):
            metadata = candidate
        else:
            for key in (
                "funnel_table",
                "funnelTable",
                "funnel_visualization",
                "funnelVisualization",
            ):
                nested = payload.get(key)
                if isinstance(nested, Mapping) and isinstance(
                    nested.get("metadata"), Mapping
                ):
                    metadata = nested["metadata"]
                    break
        sampled = bool(
            metadata.get("sampling_metadatas", metadata.get("samplingMetadatas", []))
        )
        thresholded = bool(
            metadata.get(
                "subject_to_thresholding",
                metadata.get("subjectToThresholding", False),
            )
        )
        other = bool(
            metadata.get(
                "data_loss_from_other_row",
                metadata.get("dataLossFromOtherRow", False),
            )
        )
        metrics = arguments.get("metrics", [])
        notes: list[str] = []
        if sampled:
            notes.append("sampled")
        if thresholded:
            notes.append("subject_to_thresholding")
        if other:
            notes.append("data_loss_from_other_row")
        total_rows = McpExecutionService._find_int(payload, {"rowcount", "row_count"})
        if (
            total_rows is not None
            and total_rows > McpExecutionService._ga4_returned_row_count(payload)
        ):
            # An intentionally bounded ranking is valid, but not a complete census.
            notes.append("result_row_limit_applied")
        if isinstance(metrics, list) and any(
            item in {"activeUsers", "totalUsers", "newUsers", "sessions"}
            for item in metrics
        ):
            notes.append("unique_count_may_be_approximate")
        return ReportQuality(
            sampled=sampled,
            subject_to_thresholding=thresholded,
            data_loss_from_other_row=other,
            empty_reason=McpExecutionService._optional_text(
                metadata,
                "empty_reason",
                "emptyReason",
            ),
            time_zone=McpExecutionService._optional_text(
                metadata,
                "time_zone",
                "timeZone",
            ),
            currency_code=McpExecutionService._optional_text(
                metadata,
                "currency_code",
                "currencyCode",
            ),
            notes=tuple(notes),
        )

    @staticmethod
    def _optional_text(
        payload: Mapping[str, Any],
        snake_key: str,
        camel_key: str,
    ) -> str | None:
        value = payload.get(snake_key, payload.get(camel_key))
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _property_quota(payload: Mapping[str, Any]) -> dict[str, int]:
        candidate = payload.get("property_quota", payload.get("propertyQuota"))
        if not isinstance(candidate, Mapping):
            return {}
        flattened: dict[str, int] = {}

        def visit(value: Mapping[str, Any], prefix: str = "") -> None:
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(item, Mapping):
                    visit(item, path)
                elif isinstance(item, int) and not isinstance(item, bool):
                    flattened[path] = item

        visit(candidate)
        return flattened

    async def _execute_bigquery(
        self,
        invocation: PreparedInvocation,
        profile: DataAccessProfile,
    ) -> ToolExecutionResult:
        credential = await self._bigquery_credentials.for_bigquery(profile)
        arguments = dict(invocation.arguments)
        bytes_processed: int | None = None

        if invocation.tool_name == "execute_sql_readonly":
            arguments["dryRun"] = True
            dry_run = await self._bigquery.call_tool(
                invocation.tool_name,
                arguments,
                credential,
            )
            bytes_processed = self._find_int(
                dry_run,
                {"totalbytesprocessed", "bytesprocessed", "total_bytes_processed"},
            )
            if bytes_processed is None:
                raise McpExecutionDenied("dry_run_bytes_missing")
            if bytes_processed > self._settings.bigquery_max_dry_run_bytes:
                raise McpExecutionDenied("dry_run_byte_budget_exceeded")
            arguments["dryRun"] = False

        payload = await self._bigquery.call_tool(
            invocation.tool_name,
            arguments,
            credential,
        )
        row_count = self._returned_row_count(payload)
        if row_count > self._settings.bigquery_max_result_rows:
            raise McpExecutionDenied("mcp_result_row_limit_exceeded")
        query_id = self._find_text(payload, {"queryid", "query_id", "jobid", "job_id"})
        actual_bytes = self._find_int(
            payload,
            {"totalbytesprocessed", "bytesprocessed", "total_bytes_processed"},
        )
        return ToolExecutionResult(
            payload=payload,
            query_id=query_id,
            bytes_processed=actual_bytes
            if actual_bytes is not None
            else bytes_processed,
            row_count=row_count,
        )

    async def _audit(
        self,
        invocation: PreparedInvocation,
        status: str,
        *,
        query_id: str | None = None,
        bytes_processed: int | None = None,
        row_count: int | None = None,
        result_row_limit: int | None = None,
        error_code: str | None = None,
        attempt_count: int | None = None,
        compatibility_checked: bool | None = None,
        quality: ReportQuality | None = None,
        property_quota: Mapping[str, int] | None = None,
        ga4_credential_context: Ga4CredentialContext | None = None,
    ) -> None:
        query = invocation.arguments.get("query")
        details: dict[str, Any] = {
            "mcp_server": invocation.server.value,
            "mcp_tool": invocation.tool_name,
        }
        if invocation.server is McpServer.GA4:
            requested_limit = invocation.arguments.get("limit")
            if isinstance(requested_limit, int) and not isinstance(
                requested_limit, bool
            ):
                details["requested_row_limit"] = requested_limit
            date_ranges = invocation.arguments.get("date_ranges")
            if isinstance(date_ranges, list):
                details["date_range_count"] = len(date_ranges)
            if invocation.ga4_auth_mode is not None:
                details["ga4_auth_mode"] = invocation.ga4_auth_mode.value
            property_id = invocation.arguments.get("property_id")
            if isinstance(property_id, (str, int)):
                details["ga4_property_id"] = str(property_id)
            canonical_request = json.dumps(
                invocation.arguments,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            details["query_hash"] = hashlib.sha256(
                canonical_request.encode("utf-8")
            ).hexdigest()
            if ga4_credential_context is not None:
                if ga4_credential_context.auth_mode is Ga4AuthMode.USER_OAUTH:
                    details.update(
                        {
                            "google_subject": ga4_credential_context.google_subject,
                            "granted_scopes": sorted(
                                ga4_credential_context.granted_scopes
                            ),
                            "token_created_at": (
                                ga4_credential_context.token_created_at.isoformat()
                                if ga4_credential_context.token_created_at is not None
                                else None
                            ),
                            "token_refresh_at": (
                                ga4_credential_context.token_refresh_at.isoformat()
                                if ga4_credential_context.token_refresh_at is not None
                                else None
                            ),
                        }
                    )
                else:
                    details["service_account_email"] = (
                        ga4_credential_context.service_account_email
                    )
                    details["data_access_profile_id"] = (
                        ga4_credential_context.data_access_profile_id
                    )
        if isinstance(query, str):
            details["query_hash"] = hashlib.sha256(query.encode()).hexdigest()
        if query_id is not None:
            details["query_id"] = query_id
        if bytes_processed is not None:
            details["bytes_processed"] = bytes_processed
        if row_count is not None:
            details["row_count"] = row_count
        if result_row_limit is not None:
            details["result_row_limit"] = result_row_limit
        if error_code is not None:
            details["error_code"] = error_code
        if attempt_count is not None:
            details["attempt_count"] = attempt_count
        if compatibility_checked is not None:
            details["compatibility_checked"] = compatibility_checked
        if quality is not None:
            details["report_quality"] = quality.model_dump(mode="json")
        if property_quota:
            details["property_quota"] = dict(property_quota)
        await self._repository.write_audit_event(
            AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id_context.get() or "internal",
                action="mcp.tool.execute",
                user_id=invocation.user_id,
                company_id=invocation.company_id,
                data_source_set_id=invocation.data_source_set_id,
                session_id=invocation.session_id,
                data_access_profile_id=invocation.data_access_profile_id,
                status=status,
                details=details,
            )
        )

    async def _record_ga4_property_access(
        self,
        invocation: PreparedInvocation,
        access_status: Ga4PropertyAccessStatus,
    ) -> None:
        property_id = invocation.arguments.get("property_id")
        if isinstance(property_id, (str, int)):
            await self._repository.put_ga4_property_access(
                Ga4PropertyAccessRecord(
                    user_id=invocation.user_id,
                    property_id=str(property_id),
                    status=access_status,
                )
            )

    @staticmethod
    def _ga4_returned_row_count(payload: Mapping[str, Any]) -> int:
        """GA4 rowCount is total matches, even for an empty response page."""

        report = payload
        for key in ("funnel_table", "funnelTable"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                report = nested
                break
        rows = report.get("rows")
        if rows is None:
            # Protobuf JSON omits empty repeated fields; never substitute rowCount.
            return 0
        if not isinstance(rows, list):
            raise McpExecutionDenied("mcp_rows_have_invalid_shape")
        return len(rows)

    @classmethod
    def _returned_row_count(cls, payload: Mapping[str, Any]) -> int:
        """Count rows crossing the MCP boundary, not the source's total matches."""

        rows = payload.get("rows")
        if isinstance(rows, list):
            return len(rows)
        for key in ("funnel_table", "funnelTable"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                nested_rows = nested.get("rows")
                if isinstance(nested_rows, list):
                    return len(nested_rows)
        explicit = cls._find_int(payload, {"rowcount", "row_count"})
        return explicit if explicit is not None else 0

    @classmethod
    def _find_int(cls, value: Any, keys: set[str]) -> int | None:
        found = cls._find_value(value, keys)
        if isinstance(found, bool):
            return None
        if isinstance(found, int):
            return found
        if isinstance(found, str) and found.isdigit():
            return int(found)
        return None

    @staticmethod
    def _is_transient_ga4_error(error: Exception) -> bool:
        return isinstance(
            error,
            (
                google_exceptions.DeadlineExceeded,
                google_exceptions.InternalServerError,
                google_exceptions.ServiceUnavailable,
                google_exceptions.TooManyRequests,
            ),
        )

    @classmethod
    def _find_text(cls, value: Any, keys: set[str]) -> str | None:
        found = cls._find_value(value, keys)
        return str(found) if isinstance(found, (str, int)) else None

    @classmethod
    def _find_value(cls, value: Any, keys: set[str]) -> Any:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).replace("-", "").casefold()
                if normalized in keys:
                    return nested
            for nested in value.values():
                found = cls._find_value(nested, keys)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = cls._find_value(nested, keys)
                if found is not None:
                    return found
        return None
