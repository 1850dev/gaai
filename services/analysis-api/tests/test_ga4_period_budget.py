"""Regression for actual GA4 two-period pages returning N rows per period."""

import pytest

from analytics_ai.config import Settings
from analytics_ai.mcp_gateway.executor import McpExecutionDenied, McpExecutionService
from analytics_ai.mcp_gateway.gateway import McpGateway, McpServer
from analytics_ai.orchestrator.normalization import normalize_rows
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_mcp_executor import FakeGa4, FakeUserGa4Credentials, ga4_invocation

AXES = (
    "sessionSource",
    "sessionMedium",
    "sessionDefaultChannelGroup",
    "pagePath",
    "landingPagePlusQueryString",
    "deviceCategory",
    "country",
    "userGender",
    "eventName",
    "browser",
    "yearMonth",
)


def response(field: str, *, current: int = 10, baseline: int = 10) -> dict:
    return {
        "rowCount": 10000,
        "dimensionHeaders": [{"name": field}, {"name": "dateRange"}],
        "metricHeaders": [{"name": "activeUsers"}],
        "rows": [
            {
                "dimensionValues": [{"value": str(i)}, {"value": period}],
                "metricValues": [{"value": "12"}],
            }
            for period, count in (("current", current), ("comparison", baseline))
            for i in range(count)
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", AXES)
async def test_two_period_breakdowns_accept_bounded_labelled_rows(
    repository: InMemoryRepository, field: str
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    invocation.arguments.update(
        {
            "date_ranges": [
                {
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-30",
                    "name": "current",
                },
                {
                    "start_date": "2026-05-01",
                    "end_date": "2026-05-31",
                    "name": "comparison",
                },
            ],
            "dimensions": [field],
            "limit": 10,
        }
    )
    ga4 = FakeGa4(response(field))
    executor = McpExecutionService(
        settings, repository, ga4_credentials=FakeUserGa4Credentials(), ga4_client=ga4
    )
    result = await executor.execute(invocation)
    rows = normalize_rows(result.payload)
    assert result.row_count == len(rows) == 20
    assert result.result_row_limit == 20
    assert sum(row["dateRange"] == "comparison" for row in rows) == 10
    assert ga4.calls[0][1]["property_id"] == "222222222"
    assert repository.audit_events[-1].details["requested_row_limit"] == 10
    assert repository.audit_events[-1].details["result_row_limit"] == 20
    assert "result_row_limit_applied" in result.quality.notes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["unbalanced", "unknown", "missing_header", "duplicate_name", "global_cap"],
)
async def test_comparison_never_allows_unverified_expansion(
    repository: InMemoryRepository, corruption: str
) -> None:
    settings = Settings(environment="test")
    invocation = await ga4_invocation(repository, settings)
    invocation.arguments.update(
        {"date_ranges": [{"name": "current"}, {"name": "comparison"}], "limit": 10}
    )
    payload = response("deviceCategory")
    if corruption == "unbalanced":
        payload = response("deviceCategory", current=11, baseline=9)
    elif corruption == "unknown":
        payload["rows"][0]["dimensionValues"][1]["value"] = "other"
    elif corruption == "missing_header":
        payload.pop("dimensionHeaders")
    elif corruption == "duplicate_name":
        invocation.arguments["date_ranges"][1]["name"] = "current"
    else:
        invocation.arguments["limit"] = 2000
        payload = response("deviceCategory", current=1600, baseline=1600)
    executor = McpExecutionService(
        settings,
        repository,
        ga4_credentials=FakeUserGa4Credentials(),
        ga4_client=FakeGa4(payload),
    )
    with pytest.raises(McpExecutionDenied, match="mcp_result_row_limit_exceeded"):
        await executor.execute(invocation)


@pytest.mark.asyncio
async def test_gateway_reserves_global_budget_before_comparison_call(
    repository: InMemoryRepository,
) -> None:
    settings = Settings(environment="test")
    await ga4_invocation(repository, settings)
    invocation = await McpGateway(settings, repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={
            "date_ranges": [
                {"start_date": "2026-06-01", "end_date": "2026-06-30"},
                {"start_date": "2026-05-01", "end_date": "2026-05-31"},
            ],
            "metrics": ["activeUsers"],
            "dimensions": ["deviceCategory"],
            "limit": 3000,
        },
    )
    assert invocation.arguments["limit"] == 1500
