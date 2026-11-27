from __future__ import annotations

import pytest
from google.analytics import data_v1alpha, data_v1beta

from analytics_ai.authorization.service import AccessDenied
from analytics_ai.config import Settings
from analytics_ai.domain.models import AnalysisSession, AnalysisState
from analytics_ai.mcp_gateway.gateway import McpGateway, McpServer, ToolPolicyDenied
from analytics_ai.repositories.memory import InMemoryRepository


async def add_web_session(repository: InMemoryRepository) -> None:
    await repository.create_session(
        AnalysisSession(
            id="as_web",
            user_id="user-a",
            company_id="company_a",
            data_source_set_id="company_a_web",
            data_access_profile_id="dap_company_a",
            state=AnalysisState(
                data_source_set_id="company_a_web",
                semantic_version="a-web-v1",
            ),
        )
    )


async def add_store_session(repository: InMemoryRepository) -> None:
    await repository.create_session(
        AnalysisSession(
            id="as_store",
            user_id="user-b",
            company_id="company_a",
            data_source_set_id="company_a_store",
            data_access_profile_id="dap_company_a",
            state=AnalysisState(
                data_source_set_id="company_a_store",
                semantic_version="a-store-v1",
            ),
        )
    )


@pytest.mark.asyncio
async def test_ga4_property_is_injected_not_selected_by_llm(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    invocation = await McpGateway(Settings(environment="test"), repository).prepare(
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
    assert invocation.arguments["property_id"] == "222222222"
    assert invocation.arguments["limit"] == 3_000
    scope_expressions = invocation.arguments["dimension_filter"]["and_group"][
        "expressions"
    ]
    assert [item["filter"]["field_name"] for item in scope_expressions] == [
        "platform",
        "streamId",
    ]
    assert [item["filter"]["string_filter"]["value"] for item in scope_expressions] == [
        "web",
        "3333333333",
    ]
    assert data_v1beta.FilterExpression(invocation.arguments["dimension_filter"])


@pytest.mark.asyncio
async def test_ga4_stream_scope_is_anded_with_proposed_filter(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    proposed_filter = {
        "filter": {
            "field_name": "deviceCategory",
            "string_filter": {"match_type": "EXACT", "value": "mobile"},
        }
    }
    invocation = await McpGateway(Settings(environment="test"), repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_realtime_report",
        proposed_arguments={"dimension_filter": proposed_filter},
    )

    expressions = invocation.arguments["dimension_filter"]["and_group"]["expressions"]
    assert expressions[1] == proposed_filter
    assert expressions[0]["and_group"]["expressions"][1]["filter"] == {
        "field_name": "streamId",
        "string_filter": {
            "match_type": "EXACT",
            "value": "3333333333",
            "case_sensitive": False,
        },
    }


@pytest.mark.asyncio
async def test_ga4_rejects_invalid_dimension_filter_before_mcp(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    with pytest.raises(ToolPolicyDenied, match="ga4_dimension_filter_invalid"):
        await McpGateway(Settings(environment="test"), repository).prepare(
            user_id="user-a",
            session_id="as_web",
            server=McpServer.GA4,
            tool_name="run_report",
            proposed_arguments={"dimension_filter": "streamId=other"},
        )


@pytest.mark.asyncio
async def test_ga4_result_limit_cannot_exceed_policy(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    invocation = await McpGateway(Settings(environment="test"), repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={"limit": 250_000},
    )
    assert invocation.arguments["limit"] == 3_000


@pytest.mark.asyncio
async def test_ga4_metric_ordering_must_use_selected_metric(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    gateway = McpGateway(Settings(environment="test"), repository)
    invocation = await gateway.prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={
            "metrics": ["screenPageViews"],
            "dimensions": ["landingPagePlusQueryString"],
            "limit": 10,
            "order_bys": [{"metric": {"metric_name": "screenPageViews"}, "desc": True}],
        },
    )
    assert invocation.arguments["limit"] == 10
    assert invocation.arguments["order_bys"][0]["desc"] is True

    with pytest.raises(
        ToolPolicyDenied,
        match="ga4_order_by_outside_selected_metrics",
    ):
        await gateway.prepare(
            user_id="user-a",
            session_id="as_web",
            server=McpServer.GA4,
            tool_name="run_report",
            proposed_arguments={
                "metrics": ["screenPageViews"],
                "order_bys": [
                    {"metric": {"metric_name": "totalRevenue"}, "desc": True}
                ],
            },
        )


@pytest.mark.asyncio
async def test_property_wide_ga4_source_still_enforces_result_limit(
    repository: InMemoryRepository,
) -> None:
    await add_store_session(repository)
    invocation = await McpGateway(Settings(environment="test"), repository).prepare(
        user_id="user-b",
        session_id="as_store",
        server=McpServer.GA4,
        tool_name="run_report",
        proposed_arguments={"limit": 250_000},
    )
    assert invocation.arguments["property_id"] == "111111111"
    assert invocation.arguments["limit"] == 3_000
    assert "dimension_filter" not in invocation.arguments


@pytest.mark.asyncio
async def test_stream_scope_is_injected_into_every_funnel_step(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    invocation = await McpGateway(Settings(environment="test"), repository).prepare(
        user_id="user-a",
        session_id="as_web",
        server=McpServer.GA4,
        tool_name="run_funnel_report",
        proposed_arguments={
            "funnel_steps": [
                {"name": "View", "event": "page_view"},
                {
                    "name": "Complete",
                    "filter_expression": {
                        "funnel_event_filter": {
                            "event_name": "page_view_reservation_complete"
                        }
                    },
                },
            ]
        },
    )

    assert invocation.arguments["property_id"] == "222222222"
    for step in invocation.arguments["funnel_steps"]:
        assert "event" not in step
        expression = step["filter_expression"]
        assert data_v1alpha.FunnelFilterExpression(expression)
        fixed_scope = expression["and_group"]["expressions"][0]["and_group"][
            "expressions"
        ]
        assert [item["funnel_field_filter"]["field_name"] for item in fixed_scope] == [
            "platform",
            "streamId",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "error_code"),
    [
        (
            {"segments": [{"segment_id": "app-users"}]},
            "ga4_stream_scope_unsupported_for_funnel_segments",
        ),
        (
            {"funnel_next_action": {"next_action_dimension": "eventName"}},
            "ga4_stream_scope_unsupported_for_funnel_next_action",
        ),
    ],
)
async def test_stream_scoped_funnel_rejects_unscoped_advanced_features(
    repository: InMemoryRepository,
    argument: dict[str, object],
    error_code: str,
) -> None:
    await add_web_session(repository)
    with pytest.raises(ToolPolicyDenied, match=error_code):
        await McpGateway(Settings(environment="test"), repository).prepare(
            user_id="user-a",
            session_id="as_web",
            server=McpServer.GA4,
            tool_name="run_funnel_report",
            proposed_arguments={
                "funnel_steps": [{"event": "page_view"}],
                **argument,
            },
        )


@pytest.mark.asyncio
async def test_pilot_rejects_bigquery_even_with_an_existing_grant(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    with pytest.raises(ToolPolicyDenied, match="bigquery_tools_disabled"):
        await McpGateway(
            Settings(environment="test", enable_analytics_tools=True), repository
        ).prepare(
            user_id="user-a",
            session_id="as_web",
            server=McpServer.BIGQUERY,
            tool_name="get_dataset_info",
            proposed_arguments={},
        )


@pytest.mark.asyncio
async def test_store_dataset_never_reaches_bigquery(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    with pytest.raises(ValueError, match="resource_outside_allowlist"):
        await McpGateway(
            Settings(environment="test", enable_bigquery_tools=True), repository
        ).prepare(
            user_id="user-a",
            session_id="as_web",
            server=McpServer.BIGQUERY,
            tool_name="execute_sql_readonly",
            proposed_arguments={
                "query": (
                    "SELECT event_name FROM "
                    "`company-a-data.analytics_111111111.events_*` "
                    "WHERE _TABLE_SUFFIX = '20260801' LIMIT 10"
                )
            },
        )


@pytest.mark.asyncio
async def test_other_user_cannot_use_session(
    repository: InMemoryRepository,
) -> None:
    await add_web_session(repository)
    with pytest.raises(AccessDenied):
        await McpGateway(Settings(environment="test"), repository).prepare(
            user_id="user-b",
            session_id="as_web",
            server=McpServer.GA4,
            tool_name="run_report",
            proposed_arguments={},
        )
