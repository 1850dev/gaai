from __future__ import annotations

import asyncio
import inspect

import pytest
from analytics_mcp.tools.reporting import core as core_module
from analytics_mcp.tools.reporting.core import run_report
from analytics_mcp.tools.reporting.funnel import run_funnel_report
from analytics_mcp.tools.reporting.realtime import run_realtime_report
from google.analytics import data_v1beta
from google.auth.credentials import AnonymousCredentials

from analytics_ai.ga4 import adapter as adapter_module
from analytics_ai.ga4.adapter import Ga4McpAdapter


def test_pinned_upstream_filter_contract_supports_safe_scope_injection() -> None:
    assert "dimension_filter" in inspect.signature(run_report).parameters
    assert "dimension_filter" in inspect.signature(run_realtime_report).parameters
    assert "dimension_filter" not in inspect.signature(run_funnel_report).parameters


@pytest.mark.asyncio
async def test_request_credentials_are_isolated_concurrently() -> None:
    first = AnonymousCredentials()
    second = AnonymousCredentials()

    async def fake_tool(property_id: str) -> dict[str, int | str]:
        await asyncio.sleep(0)
        current = adapter_module._request_credentials()
        await asyncio.sleep(0)
        return {"property_id": property_id, "credential_id": id(current)}

    adapter = Ga4McpAdapter(tools={"run_report": fake_tool})
    one, two = await asyncio.gather(
        adapter.call("run_report", {"property_id": "111"}, first),
        adapter.call("run_report", {"property_id": "222"}, second),
    )
    assert one == {"property_id": "111", "credential_id": id(first)}
    assert two == {"property_id": "222", "credential_id": id(second)}


@pytest.mark.asyncio
async def test_pinned_official_report_preserves_limit_and_does_not_auto_paginate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[data_v1beta.RunReportRequest] = []
    credentials = AnonymousCredentials()

    class FakeDataClient:
        def run_report(
            self, request: data_v1beta.RunReportRequest
        ) -> data_v1beta.RunReportResponse:
            assert adapter_module._request_credentials() is credentials
            requests.append(request)
            return data_v1beta.RunReportResponse(
                row_count=15_000,
                rows=[
                    data_v1beta.Row(
                        dimension_values=[data_v1beta.DimensionValue(value=str(index))],
                        metric_values=[data_v1beta.MetricValue(value="100")],
                    )
                    for index in range(10)
                ],
            )

    monkeypatch.setattr(core_module, "create_data_api_client", FakeDataClient)
    adapter = Ga4McpAdapter(tools={"run_report": run_report})

    result = await adapter.call(
        "run_report",
        {
            "property_id": "222222222",
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
            "dimensions": ["pagePath"],
            "metrics": ["screenPageViews"],
            "limit": 10,
        },
        credentials,
    )

    assert len(requests) == 1
    assert requests[0].limit == 10
    assert requests[0].property == "properties/222222222"
    assert [item.name for item in requests[0].date_ranges] == ["current", "comparison"]
    assert result["row_count"] == 15_000
    assert len(result["rows"]) == 10
    with pytest.raises(LookupError):
        adapter_module._credential_context.get()
