from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from google.api_core import exceptions as google_exceptions
from google.auth.credentials import AnonymousCredentials, Credentials

from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    DataAccessProfile,
    DataSourceSet,
    Ga4AuthMode,
)
from analytics_ai.ga4.credentials import Ga4CredentialContext, Ga4CredentialError
from analytics_ai.mcp_gateway.executor import (
    McpExecutionDenied,
    McpExecutionService,
)
from analytics_ai.mcp_gateway.gateway import McpGateway, McpServer
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_mcp_gateway import add_store_session, add_web_session


class FakeCredentials:
    def __init__(self) -> None:
        self.ga4_profiles: list[str] = []
        self.bigquery_profiles: list[str] = []

    async def for_ga4(self, profile: DataAccessProfile) -> Credentials:
        self.ga4_profiles.append(profile.id)
        return AnonymousCredentials()

    async def for_bigquery(self, profile: DataAccessProfile) -> Credentials:
        self.bigquery_profiles.append(profile.id)
        return AnonymousCredentials()


class FakeUserGa4Credentials:
    def __init__(self) -> None:
        self.user_ids: list[str] = []

    async def get_credentials(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext:
        self.user_ids.append(user_id)
        assert source_set.ga4 is not None
        assert source_set.ga4.auth_mode is Ga4AuthMode.USER_OAUTH
        return Ga4CredentialContext(
            credentials=AnonymousCredentials(),
            auth_mode=Ga4AuthMode.USER_OAUTH,
            google_subject=f"google-{user_id}",
            google_email=f"{user_id}@example.com",
            granted_scopes=frozenset(
                {"https://www.googleapis.com/auth/analytics.readonly"}
            ),
        )


class TaggedCredentials(AnonymousCredentials):
    def __init__(self, user_id: str) -> None:
        super().__init__()
        self.user_id = user_id


class TaggedUserGa4Credentials:
    async def get_credentials(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext:
        return Ga4CredentialContext(
            credentials=TaggedCredentials(user_id),
            auth_mode=Ga4AuthMode.USER_OAUTH,
            google_subject=f"google-{user_id}",
            google_email=f"{user_id}@example.com",
        )


class MissingUserGa4Credentials:
    async def get_credentials(
        self,
        *,
        user_id: str,
        source_set: DataSourceSet,
        profile: DataAccessProfile | None,
    ) -> Ga4CredentialContext:
        raise Ga4CredentialError("ga4_user_connection_required")


class FakeGa4:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = response or {
            "row_count": 1,
            "rows": [{"metric_values": [{"value": "12"}]}],
        }

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        _credentials: Credentials,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        return self.response


class CompatibleGa4(FakeGa4):
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        compatibility: str = "COMPATIBLE",
    ) -> None:
        super().__init__(response)
        self.compatibility = compatibility
        self.compatibility_calls: list[dict[str, Any]] = []

    async def check_compatibility(
        self,
        arguments: Mapping[str, Any],
        _credentials: Credentials,
    ) -> dict[str, Any]:
        self.compatibility_calls.append(dict(arguments))
        return {
            "dimension_compatibilities": [],
            "metric_compatibilities": [
                {
                    "metric_metadata": {"api_name": "activeUsers"},
                    "compatibility": self.compatibility,
                },
            ],
        }


class FakeBigQuery:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self,
        _tool_name: str,
        arguments: Mapping[str, Any],
        _credentials: Credentials,
    ) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FlakyGa4(FakeGa4):
    def __init__(self, failures: list[Exception]) -> None:
        super().__init__()
        self.failures = list(failures)

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        _credentials: Credentials,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        if self.failures:
            raise self.failures.pop(0)
        return self.response


class SlowGa4(FakeGa4):
    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        _credentials: Credentials,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        await asyncio.sleep(1)
        return self.response


class ConcurrentGa4(FakeGa4):
    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[tuple[str, str]] = []

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: Credentials,
    ) -> dict[str, Any]:
        assert isinstance(credentials, TaggedCredentials)
        await asyncio.sleep(0)
        self.scopes.append((credentials.user_id, str(arguments["property_id"])))
        return self.response


async def bigquery_invocation(
    repository: InMemoryRepository,
    settings: Settings,
):
    await add_web_session(repository)
    return await McpGateway(settings, repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.BIGQUERY,
        tool_name="execute_sql_readonly",
        proposed_arguments={
            "query": (
                "SELECT event_name, COUNT(1) AS events FROM "
                "`company-a-data.analytics_222222222.events_*` "
                "WHERE _TABLE_SUFFIX BETWEEN '20260801' AND '20260824' "
                "GROUP BY event_name LIMIT 10"
            )
        },
    )


async def ga4_invocation(
    repository: InMemoryRepository,
    settings: Settings,
):
    await add_web_session(repository)
    return await McpGateway(settings, repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={
            "date_ranges": [{"start_date": "2026-08-01", "end_date": "2026-08-24"}],
            "dimensions": [],
            "metrics": ["activeUsers"],
        },
    )


@pytest.mark.asyncio
async def test_pilot_executor_rejects_a_prepared_bigquery_invocation(
    repository: InMemoryRepository,
) -> None:
    invocation = await bigquery_invocation(
        repository, Settings(environment="test", enable_bigquery_tools=True)
    )
    credentials, transport = FakeCredentials(), FakeBigQuery([])
    service = McpExecutionService(
        Settings(environment="test", enable_analytics_tools=True),
        repository,
        credentials=credentials,
        ga4_client=FakeGa4(),
        bigquery_client=transport,
    )
    with pytest.raises(McpExecutionDenied, match="bigquery_tools_disabled"):
        await service.execute(invocation)
    assert not credentials.bigquery_profiles
    assert not transport.calls


@pytest.mark.asyncio
async def test_bigquery_dry_run_precedes_execution_and_is_audited(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(
        environment="test", enable_bigquery_tools=True, bigquery_max_dry_run_bytes=100
    )
    invocation = await bigquery_invocation(repository, settings)
    transport = FakeBigQuery(
        [
            {"totalBytesProcessed": "50"},
            {
                "queryId": "query-123",
                "totalBytesProcessed": "55",
                "rows": [{"event_name": "purchase", "events": 3}],
            },
        ]
    )
    credentials = FakeCredentials()
    service = McpExecutionService(
        settings,
        repository,
        credentials=credentials,
        ga4_client=FakeGa4(),
        bigquery_client=transport,
    )

    result = await service.execute(invocation)

    assert [call["dryRun"] for call in transport.calls] == [True, False]
    assert credentials.bigquery_profiles == ["dap_company_a"]
    assert result.query_id == "query-123"
    assert result.bytes_processed == 55
    assert result.row_count == 1
    event = repository.audit_events[-1]
    assert event.status == "success"
    assert event.details["bytes_processed"] == 55
    assert "query" not in event.details


@pytest.mark.asyncio
async def test_bigquery_budget_rejection_never_executes_live_query(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(
        environment="test", enable_bigquery_tools=True, bigquery_max_dry_run_bytes=100
    )
    invocation = await bigquery_invocation(repository, settings)
    transport = FakeBigQuery([{"totalBytesProcessed": "101"}])
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_client=FakeGa4(),
        bigquery_client=transport,
    )

    with pytest.raises(McpExecutionDenied, match="dry_run_byte_budget_exceeded"):
        await service.execute(invocation)

    assert len(transport.calls) == 1
    assert transport.calls[0]["dryRun"] is True
    assert repository.audit_events[-1].status == "error"


@pytest.mark.asyncio
async def test_bigquery_ambiguous_execution_failure_is_never_retried(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(
        environment="test", enable_bigquery_tools=True, bigquery_max_dry_run_bytes=100
    )
    invocation = await bigquery_invocation(repository, settings)
    transport = FakeBigQuery(
        [
            {"totalBytesProcessed": "50"},
            google_exceptions.ServiceUnavailable("execution status unknown"),
        ]
    )
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_client=FakeGa4(),
        bigquery_client=transport,
    )

    with pytest.raises(google_exceptions.ServiceUnavailable):
        await service.execute(invocation)

    assert [call["dryRun"] for call in transport.calls] == [True, False]
    assert repository.audit_events[-1].status == "error"


@pytest.mark.asyncio
async def test_ga4_execution_uses_profile_credential_and_injected_property(
    repository: InMemoryRepository,
) -> None:
    source_set = repository.source_sets["company_a_web"]
    assert source_set.ga4 is not None
    repository.source_sets[source_set.id] = source_set.model_copy(
        update={
            "ga4": source_set.ga4.model_copy(
                update={"auth_mode": Ga4AuthMode.SERVICE_ACCOUNT}
            )
        }
    )
    await add_web_session(repository)
    settings = Settings(environment="test")
    invocation = await McpGateway(settings, repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={
            "property_id": "111111111",
            "date_ranges": [{"start_date": "2026-08-01", "end_date": "2026-08-24"}],
            "dimensions": [],
            "metrics": ["activeUsers"],
        },
    )
    credentials = FakeCredentials()
    ga4 = FakeGa4()
    service = McpExecutionService(
        settings,
        repository,
        credentials=credentials,
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    result = await service.execute(invocation)

    assert credentials.ga4_profiles == ["dap_company_a"]
    assert ga4.calls[0][1]["property_id"] == "222222222"
    assert (
        ga4.calls[0][1]["dimension_filter"] == invocation.arguments["dimension_filter"]
    )
    assert result.row_count == 1
    event = repository.audit_events[-1]
    assert event.details["ga4_property_id"] == "222222222"
    assert len(event.details["query_hash"]) == 64
    assert "dimension_filter" not in event.details


@pytest.mark.asyncio
async def test_ga4_core_report_checks_compatibility_and_records_quality(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    ga4 = CompatibleGa4(
        {
            "rows": [{"metric_values": [{"value": "12"}]}],
            "metadata": {
                "subject_to_thresholding": True,
                "time_zone": "Asia/Tokyo",
                "currency_code": "JPY",
            },
            "property_quota": {"tokens_per_day": {"remaining": 123}},
        }
    )
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    result = await service.execute(invocation)

    assert result.compatibility_checked is True
    assert ga4.compatibility_calls[0]["property_id"] == "222222222"
    assert result.quality.subject_to_thresholding is True
    assert result.quality.time_zone == "Asia/Tokyo"
    assert result.property_quota == {"tokens_per_day.remaining": 123}
    audit = repository.audit_events[-1].details
    assert audit["compatibility_checked"] is True
    assert audit["report_quality"]["subject_to_thresholding"] is True


@pytest.mark.asyncio
async def test_ga4_incompatible_report_is_denied_before_tool_execution(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    ga4 = CompatibleGa4(compatibility="INCOMPATIBLE")
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    with pytest.raises(McpExecutionDenied, match="ga4_metric_dimension_incompatible"):
        await service.execute(invocation)

    assert ga4.calls == []
    assert repository.audit_events[-1].status == "error"


@pytest.mark.asyncio
async def test_ga4_compatibility_ignores_unrequested_incompatible_fields(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    ga4 = CompatibleGa4()

    async def check_compatibility(
        arguments: Mapping[str, Any],
        _credentials: Credentials,
    ) -> dict[str, Any]:
        ga4.compatibility_calls.append(dict(arguments))
        return {
            "dimension_compatibilities": [
                {
                    "dimension_metadata": {"api_name": "campaignName"},
                    "compatibility": "INCOMPATIBLE",
                }
            ],
            "metric_compatibilities": [
                {
                    "metric_metadata": {"api_name": "advertiserAdCost"},
                    "compatibility": "INCOMPATIBLE",
                },
                {
                    "metric_metadata": {"api_name": "activeUsers"},
                    "compatibility": "COMPATIBLE",
                },
            ],
        }

    ga4.check_compatibility = check_compatibility  # type: ignore[method-assign]
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    result = await service.execute(invocation)

    assert result.compatibility_checked is True
    assert len(ga4.calls) == 1


@pytest.mark.asyncio
async def test_ga4_executor_rejects_result_over_row_policy(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    settings = Settings(environment="test")
    invocation = await McpGateway(settings, repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={
            "date_ranges": [{"start_date": "2026-08-01", "end_date": "2026-08-24"}],
            "dimensions": [],
            "metrics": ["activeUsers"],
        },
    )
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=FakeGa4(
            {"row_count": 3_001, "rows": [{"value": index} for index in range(3_001)]}
        ),
        bigquery_client=FakeBigQuery([]),
    )

    with pytest.raises(McpExecutionDenied, match="mcp_result_row_limit_exceeded"):
        await service.execute(invocation)

    assert repository.audit_events[-1].status == "error"


@pytest.mark.asyncio
async def test_ga4_executor_uses_returned_rows_not_total_matching_rows(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    invocation.arguments["limit"] = 10
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=FakeGa4(
            {
                "row_count": 15_000,
                "rows": [{"value": index} for index in range(10)],
            }
        ),
        bigquery_client=FakeBigQuery([]),
    )

    result = await service.execute(invocation)

    assert result.row_count == 10
    assert "result_row_limit_applied" in result.quality.notes
    assert repository.audit_events[-1].details["row_count"] == 10


@pytest.mark.asyncio
async def test_ga4_empty_page_does_not_treat_total_matches_as_returned_rows(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    invocation.arguments.update({"limit": 10, "offset": 15_000})
    service = McpExecutionService(
        settings,
        repository,
        ga4_credentials=FakeUserGa4Credentials(),
        # Protobuf JSON legitimately omits the empty repeated rows field.
        ga4_client=FakeGa4({"rowCount": 15_000}),
    )

    result = await service.execute(invocation)

    assert result.row_count == 0
    assert "result_row_limit_applied" in result.quality.notes


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [{"hidden": "not a row list"}, "bad", 1])
async def test_ga4_invalid_rows_cannot_bypass_result_policy(
    repository: InMemoryRepository,
    rows: object,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    service = McpExecutionService(
        settings,
        repository,
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=FakeGa4({"rows": rows, "rowCount": 0}),
    )

    with pytest.raises(McpExecutionDenied, match="mcp_rows_have_invalid_shape"):
        await service.execute(invocation)


@pytest.mark.asyncio
async def test_ga4_first_half_daily_comparison_counts_both_periods_safely(
    repository: InMemoryRepository,
) -> None:
    from datetime import date, timedelta

    from analytics_ai.orchestrator.normalization import normalize_rows

    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    invocation.arguments.update(
        {
            "date_ranges": [
                {
                    "start_date": "2026-01-01",
                    "end_date": "2026-06-30",
                    "name": "current",
                },
                {
                    "start_date": "2025-01-01",
                    "end_date": "2025-06-30",
                    "name": "comparison",
                },
            ],
            "dimensions": ["date"],
            "metrics": ["screenPageViews", "activeUsers"],
            "limit": 1_500,
        }
    )
    response_rows = [
        {
            "dimensionValues": [
                {"value": (date(year, 1, 1) + timedelta(days=day)).strftime("%Y%m%d")},
                {"value": name},
            ],
            "metricValues": [{"value": "100"}, {"value": "40"}],
        }
        for name, year in (("current", 2026), ("comparison", 2025))
        for day in range(181)
    ]
    ga4 = FakeGa4(
        {
            "rowCount": 362,
            "dimensionHeaders": [{"name": "date"}, {"name": "dateRange"}],
            "metricHeaders": [{"name": "screenPageViews"}, {"name": "activeUsers"}],
            "rows": response_rows,
        }
    )
    service = McpExecutionService(
        settings,
        repository,
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
    )

    result = await service.execute(invocation)
    normalized = normalize_rows(result.payload)

    assert result.row_count == len(normalized) == 362
    assert sum(row["dateRange"] == "current" for row in normalized) == 181
    assert sum(row["dateRange"] == "comparison" for row in normalized) == 181
    assert "result_row_limit_applied" not in result.quality.notes
    assert ga4.calls[0][1]["property_id"] == "222222222"
    assert repository.audit_events[-1].details["date_range_count"] == 2
    assert repository.audit_events[-1].details["requested_row_limit"] == 1_500


@pytest.mark.asyncio
async def test_ga4_comparison_cannot_multiply_the_allowed_result_rows(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    invocation.arguments.update(
        {
            "date_ranges": [
                {
                    "start_date": "2026-01-01",
                    "end_date": "2026-06-30",
                    "name": "current",
                },
                {
                    "start_date": "2025-01-01",
                    "end_date": "2025-06-30",
                    "name": "comparison",
                },
            ],
            "limit": 10,
        }
    )
    ga4 = FakeGa4({"rows": [{"value": index} for index in range(20)]})
    service = McpExecutionService(
        settings,
        repository,
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
    )

    with pytest.raises(McpExecutionDenied, match="mcp_result_row_limit_exceeded"):
        await service.execute(invocation)

    assert len(ga4.calls) == 1
    audit = repository.audit_events[-1]
    assert audit.status == "error"
    assert audit.details["row_count"] == 20
    assert audit.details["requested_row_limit"] == 10
    assert audit.details["result_row_limit"] == 10
    assert audit.details["date_range_count"] == 2
    assert "rows" not in audit.details


@pytest.mark.asyncio
async def test_ga4_retries_only_transient_read_only_failure(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(
        environment="test",
        ga4_max_attempts=3,
        ga4_retry_base_seconds=0,
    )
    invocation = await ga4_invocation(repository, settings)
    ga4 = FlakyGa4([google_exceptions.ServiceUnavailable("temporary")])
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    result = await service.execute(invocation)

    assert result.attempts == 2
    assert len(ga4.calls) == 2
    assert [event.status for event in repository.audit_events[-2:]] == [
        "retry",
        "success",
    ]
    assert repository.audit_events[-1].details["attempt_count"] == 2


@pytest.mark.asyncio
async def test_ga4_does_not_retry_permanent_failure(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(
        environment="test",
        ga4_max_attempts=3,
        ga4_retry_base_seconds=0,
    )
    invocation = await ga4_invocation(repository, settings)
    ga4 = FlakyGa4([google_exceptions.BadRequest("invalid")])
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    with pytest.raises(google_exceptions.BadRequest):
        await service.execute(invocation)

    assert len(ga4.calls) == 1
    assert repository.audit_events[-1].status == "error"


@pytest.mark.asyncio
async def test_executor_timeout_is_typed_and_audited(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test").model_copy(
        update={"external_request_timeout_seconds": 0.001}
    )
    invocation = await ga4_invocation(repository, settings)
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=SlowGa4(),
        bigquery_client=FakeBigQuery([]),
    )

    with pytest.raises(McpExecutionDenied, match="mcp_execution_timeout"):
        await service.execute(invocation)

    assert repository.audit_events[-1].details["error_code"] == (
        "mcp_execution_timeout"
    )


@pytest.mark.asyncio
async def test_user_oauth_never_falls_back_to_service_account(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    service_accounts = FakeCredentials()
    ga4 = FakeGa4()
    service = McpExecutionService(
        settings,
        repository,
        credentials=service_accounts,
        ga4_credentials=MissingUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    with pytest.raises(McpExecutionDenied, match="ga4_user_connection_required"):
        await service.execute(invocation)

    assert service_accounts.ga4_profiles == []
    assert ga4.calls == []
    assert repository.audit_events[-1].details["ga4_auth_mode"] == "USER_OAUTH"


@pytest.mark.asyncio
async def test_concurrent_users_keep_distinct_oauth_credentials_and_properties(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    await add_web_session(repository)
    await add_store_session(repository)
    gateway = McpGateway(settings, repository)
    web_invocation, store_invocation = await asyncio.gather(
        gateway.prepare(
            user_id="user-a",
            session_id="as_web",
            server=McpServer.GA4,
            tool_name="run_report",
            proposed_arguments={},
        ),
        gateway.prepare(
            user_id="user-b",
            session_id="as_store",
            server=McpServer.GA4,
            tool_name="run_report",
            proposed_arguments={},
        ),
    )
    ga4 = ConcurrentGa4()
    service = McpExecutionService(
        settings,
        repository,
        credentials=FakeCredentials(),
        ga4_credentials=TaggedUserGa4Credentials(),
        ga4_client=ga4,
        bigquery_client=FakeBigQuery([]),
    )

    await asyncio.gather(
        service.execute(web_invocation),
        service.execute(store_invocation),
    )

    assert set(ga4.scopes) == {
        ("user-a", "222222222"),
        ("user-b", "111111111"),
    }
    successful = [
        event for event in repository.audit_events if event.status == "success"
    ]
    assert {
        (event.user_id, event.details.get("google_subject")) for event in successful
    } >= {
        ("user-a", "google-user-a"),
        ("user-b", "google-user-b"),
    }
