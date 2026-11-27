from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from analytics_ai.api.app import create_app
from analytics_ai.auth.firebase import VerifiedIdentity
from analytics_ai.cache.store import (
    DatasetCache,
    InMemoryCacheBlobs,
    InMemoryCacheMetadata,
)
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    AnalysisSession,
    AnalysisState,
    CalculationKind,
    ComparisonKind,
    SemanticDefinition,
    SemanticScope,
)
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import McpGateway, PreparedInvocation
from analytics_ai.orchestrator.answering import AnswerDraft
from analytics_ai.orchestrator.planning import (
    ComparisonIntent,
    IntentPlan,
    SummaryStatistic,
    TimeGrain,
)
from analytics_ai.orchestrator.service import (
    AnalysisOrchestrationError,
    AnalysisOrchestrator,
)
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.repositories.memory import InMemoryRepository


class QueuePlanner:
    def __init__(self, plans: list[IntentPlan]) -> None:
        self.plans = list(plans)

    async def plan(self, **_arguments: Any) -> IntentPlan:
        return self.plans.pop(0)


class SlowPlanner:
    async def plan(self, **_arguments: Any) -> IntentPlan:
        await asyncio.sleep(1)
        return active_users_plan()


class StaticAnswerComposer:
    model_name = "answer-model-test"

    async def compose(self, **arguments: Any) -> AnswerDraft:
        findings = arguments["findings"]
        return AnswerDraft(
            headline="利用状況の変化を確認しました",
            finding_ids=(findings[0].id,),
            next_question="チャネル別に確認しますか？",
        )


class InvalidAnswerComposer:
    model_name = "invalid-answer-model-test"

    async def compose(self, **_arguments: Any) -> AnswerDraft:
        return AnswerDraft(
            headline="変化を確認しました",
            finding_ids=("finding_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
        )


class HangingAnswerComposer:
    model_name = "hanging-answer-model-test"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def compose(self, **_arguments: Any) -> AnswerDraft:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class RecordingExecutor:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.invocations: list[PreparedInvocation] = []

    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult:
        self.invocations.append(invocation)
        return ToolExecutionResult(
            payload=self.payload,
            query_id=None,
            bytes_processed=None,
            row_count=len(self.payload.get("rows", [])),
        )


class StaticVerifier:
    async def verify(self, token: str) -> VerifiedIdentity:
        return VerifiedIdentity(uid=token, email=f"{token}@example.com")


async def add_session(repository: InMemoryRepository) -> AnalysisSession:
    session = AnalysisSession(
        id="as_orchestrator",
        user_id="user-a",
        company_id="company_a",
        data_source_set_id="company_a_web",
        data_access_profile_id="dap_company_a",
        state=AnalysisState(
            data_source_set_id="company_a_web",
            semantic_version="a-web-v1",
        ),
    )
    await repository.create_session(session)
    return session


def build_orchestrator(
    repository: InMemoryRepository,
    plans: list[IntentPlan],
    payload: dict[str, Any],
    *,
    answer_composer: Any | None = None,
    answer_composition_timeout_seconds: float = 30.0,
) -> tuple[AnalysisOrchestrator, RecordingExecutor]:
    executor = RecordingExecutor(payload)
    return (
        AnalysisOrchestrator(
            repository,
            planner=QueuePlanner(plans),
            gateway=McpGateway(Settings(environment="test"), repository),
            executor=executor,
            cache=DatasetCache(InMemoryCacheMetadata(), InMemoryCacheBlobs()),
            answer_composer=answer_composer,
            answer_composition_timeout_seconds=answer_composition_timeout_seconds,
        ),
        executor,
    )


def active_users_plan(*, visualize: bool = False) -> IntentPlan:
    if visualize:
        return IntentPlan(intent="visualize", visualization_only=True)
    return IntentPlan(
        intent="active users compared with previous month",
        period={"start": date(2026, 8, 1), "end": date(2026, 8, 24)},
        comparison=ComparisonIntent.PREVIOUS_MONTH,
        metric_terms=("UU",),
    )


def ga4_comparison_payload() -> dict[str, Any]:
    return {
        "dimension_headers": [{"name": "dateRange"}],
        "metric_headers": [{"name": "activeUsers", "type": "TYPE_INTEGER"}],
        "rows": [
            {
                "dimension_values": [{"value": "current"}],
                "metric_values": [{"value": "120"}],
            },
            {
                "dimension_values": [{"value": "comparison"}],
                "metric_values": [{"value": "100"}],
            },
        ],
        "row_count": 2,
    }


def test_browser_qwen_authorized_report_without_cloud_inference(
    repository: InMemoryRepository,
) -> None:
    # An empty planner queue and a composer spy catch any hidden cloud call.
    composer = HangingAnswerComposer()
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload(), answer_composer=composer
    )
    api = TestClient(
        create_app(
            Settings(
                environment="test",
                enable_analytics_tools=True,
                enable_browser_qwen=True,
                enable_browser_agent=True,
                vertex_user_ids=frozenset({"user-b"}),
            ),
            repository=repository,
            token_verifier=StaticVerifier(),
            orchestrator=orchestrator,
        )
    )
    auth = {"Authorization": "Bearer user-a", "Idempotency-Key": "browser-turn"}
    assert (
        api.get("/api/me", headers=auth).json()["analysis_provider"] == "browser_qwen"
    )
    other = {"Authorization": "Bearer user-b", "Idempotency-Key": "other-turn"}
    assert "analysis_provider" not in api.get("/api/me", headers=other).json()
    session = api.post(
        "/api/sessions", headers=auth, json={"data_source_set_id": "company_a_web"}
    ).json()
    path = f"/api/sessions/{session['id']}"
    assert api.get(path + "/browser/context").status_code == 401
    assert api.get(path + "/browser/context", headers=other).status_code == 403
    ctx = api.get(path + "/browser/context", headers=auth).json()
    assert "active_users" in [term["name"] for term in ctx["terms"]]
    for route in ("messages", "messages/stream", "agent/stream"):
        assert (
            api.post(
                path + "/" + route, headers=auth, json={"content": "test"}
            ).status_code
            == 403
        )
    plan = {
        "skill_id": "ga4.compare.v1",
        "metric": "active_users",
        "period": {"start": "2026-08-01", "end": "2026-08-24"},
        "comparison_period": {"start": "2026-07-01", "end": "2026-07-24"},
    }
    body = {"content": "8月と7月のUU比較", "plan": plan, "revision": ctx["revision"]}
    assert (
        api.post(path + "/browser/analyze", headers=other, json=body).status_code == 403
    )
    assert (
        api.post(
            path + "/browser/analyze",
            headers=auth,
            json={**body, "plan": {**plan, "property_id": "111111111"}},
        ).status_code
        == 422
    )
    assert (
        api.post(
            path + "/browser/analyze",
            headers=auth,
            json={**body, "revision": ctx["revision"] + 1},
        ).status_code
        == 409
    )
    assert not executor.invocations
    assert (
        api.post(
            "/api/schedules",
            headers=auth,
            json={
                "name": "free-account-test",
                "prompt": "daily PV",
                "data_source_set_id": "company_a_web",
                "enabled": True,
            },
        ).status_code
        == 403
    )
    response = api.post(path + "/browser/analyze", headers=auth, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(executor.invocations) == 1 and not composer.started.is_set()
    assert executor.invocations[0].arguments["property_id"] == "222222222"
    assert any(
        event.details.get("plan_origin") == "browser_confirmed"
        for event in repository.audit_events
    )
    assert result["evidence"] and result["findings"] and result["visualization"]
    assert result["user_message"]["role"] == "user"
    history = api.get(path + "/history", headers=auth).json()
    assert history["latest_result"]["rows"] == result["rows"]
    assert history["latest_result"]["visualization"] == result["visualization"]
    assert (
        api.post(path + "/browser/analyze", headers=auth, json=body).status_code == 409
    )
    assert len(executor.invocations) == 1
    with api.websocket_connect(
        path + "/browser/agent", headers={"origin": "http://localhost:5173"}
    ) as socket:
        socket.send_json(
            {
                "kind": "start",
                "token": "user-a",
                "content": (
                    "追加取得は不要です。ここまでの結果をレポートにまとめてください。"
                ),
                "operation_id": "saved-browser-report",
            }
        )
        while (frame := socket.receive_json())["event"] != "done":
            assert frame["event"] not in {"inference", "analysis", "error"}, frame
        report_id = frame["data"]["report_message_id"]
        assert report_id and frame["data"]["completed_queries"] == 0
    after = api.get(path + "/history", headers=auth).json()
    report = next(m for m in after["messages"] if m["id"] == report_id)
    assert report["evidence_ids"] == [e["id"] for e in result["evidence"]]
    assert any(chart["parent_message_id"] == report_id for chart in after["results"])
    assert len(executor.invocations) == 1 and not composer.started.is_set()


def test_browser_pv_ranking_uses_page_axis_ga4_limit_and_persisted_table(
    repository: InMemoryRepository,
) -> None:
    for name, kind, mapping in (
        ("page_views", "metric", {"metric": "screenPageViews"}),
        ("page_path", "dimension", {"dimension": "pagePathPlusQueryString"}),
    ):
        repository.semantic_definitions[name] = SemanticDefinition(
            id=name,
            canonical_name=name,
            kind=kind,
            ga4_mapping=mapping,
            aliases=(name,),
            scope=SemanticScope.GLOBAL,
            version="a-web-v1",
        )
    payload = {
        "dimension_headers": [{"name": "pagePathPlusQueryString"}],
        "metric_headers": [{"name": "screenPageViews", "type": "TYPE_INTEGER"}],
        "rows": [
            {
                "dimension_values": [{"value": f"/page-{idx}"}],
                "metric_values": [{"value": str(100 - idx)}],
            }
            for idx in range(10)
        ],
        "row_count": 20,
    }
    orchestrator, executor = build_orchestrator(repository, [], payload)
    api = TestClient(
        create_app(
            Settings(
                environment="test",
                enable_analytics_tools=True,
                enable_browser_qwen=True,
            ),
            repository=repository,
            token_verifier=StaticVerifier(),
            orchestrator=orchestrator,
        )
    )
    headers = {"Authorization": "Bearer user-a", "Idempotency-Key": "rank-turn"}
    session = api.post(
        "/api/sessions", headers=headers, json={"data_source_set_id": "company_a_web"}
    ).json()
    path = f"/api/sessions/{session['id']}"
    response = api.post(
        path + "/browser/analyze",
        headers=headers,
        json={
            "revision": session["revision"],
            "content": "2026年11月のPVランキングを上位10件で表にしてください。",
            "plan": {
                "skill_id": "ga4.breakdown_rank.v1",
                "metric": "page_views",
                "dimension": "page_path",
                "period": {"start": "2026-11-01", "end": "2026-11-30"},
                "limit": 10,
                "presentation": "table",
            },
        },
    )
    assert response.status_code == 200, response.text
    arguments = executor.invocations[0].arguments
    assert arguments["dimensions"] == ["pagePathPlusQueryString"]
    assert arguments["metrics"] == ["screenPageViews"]
    assert arguments["limit"] == 10
    assert arguments["order_bys"] == [
        {"metric": {"metric_name": "screenPageViews"}, "desc": True}
    ]
    result = response.json()
    assert result["visualization"]["type"] == "table" and len(result["rows"]) == 10
    assert (
        api.get(path + "/history", headers=headers).json()["latest_result"][
            "visualization"
        ]["type"]
        == "table"
    )


def test_category_calculations_rank_and_contribution_are_deterministic() -> None:
    current = [
        {"channel": "Organic", "sessions": 140},
        {"channel": "Paid", "sessions": 60},
    ]
    comparison = [
        {"channel": "Organic", "sessions": 100},
        {"channel": "Paid", "sessions": 50},
    ]

    facts = AnalysisOrchestrator._calculation_facts(
        current_rows=current,
        comparison_rows=comparison,
        metric_field="sessions",
        dimension_field="channel",
        metric_aggregation="sum",
        value=Decimal("140"),
        comparison_value=Decimal("100"),
        comparison_kind=ComparisonKind.MOM,
    )

    ranks = [item for item in facts if item.kind is CalculationKind.RANK]
    contributions = {
        item.category: item
        for item in facts
        if item.kind is CalculationKind.CONTRIBUTION
    }
    assert [(item.category, item.rank) for item in ranks] == [
        ("Organic", 1),
        ("Paid", 2),
    ]
    assert contributions["Organic"].value == "0.8"
    assert contributions["Paid"].value == "0.2"


def test_non_additive_metric_never_gets_contribution_calculation() -> None:
    facts = AnalysisOrchestrator._calculation_facts(
        current_rows=[{"channel": "Organic", "activeUsers": 100}],
        comparison_rows=[{"channel": "Organic", "activeUsers": 80}],
        metric_field="activeUsers",
        dimension_field="channel",
        metric_aggregation=None,
        value=Decimal("100"),
        comparison_value=Decimal("80"),
        comparison_kind=ComparisonKind.MOM,
    )

    assert all(item.kind is not CalculationKind.CONTRIBUTION for item in facts)


def test_contribution_does_not_infer_zero_for_missing_categories() -> None:
    facts = AnalysisOrchestrator._calculation_facts(
        current_rows=[{"channel": "New", "sessions": 30}],
        comparison_rows=[{"channel": "Gone", "sessions": 10}],
        metric_field="sessions",
        dimension_field="channel",
        metric_aggregation="sum",
        value=Decimal("30"),
        comparison_value=Decimal("10"),
        comparison_kind=ComparisonKind.MOM,
    )

    contributions = {
        item.category: item.value
        for item in facts
        if item.kind is CalculationKind.CONTRIBUTION
    }
    assert contributions == {}  # A limited response cannot establish new/gone.


def test_anomaly_score_is_calculated_in_code_from_median_and_mad() -> None:
    fact = AnalysisOrchestrator._anomaly_fact(
        [
            {"date": f"2026080{index}", "activeUsers": value}
            for index, value in enumerate((10, 11, 9, 10, 12, 10, 40), start=1)
        ],
        metric_field="activeUsers",
        dimension_field="date",
    )

    assert fact is not None
    assert fact.kind is CalculationKind.ANOMALY_SCORE
    assert Decimal(fact.value) > Decimal("3.5")


def test_time_series_summary_distinguishes_total_average_and_latest() -> None:
    rows = [
        {"date": "20260801", "sessions": 10},
        {"date": "20260802", "sessions": 20},
    ]

    total = AnalysisOrchestrator._select_value(
        rows,
        metric_field="sessions",
        dimension_field="date",
        dimension_kind="date",
        metric_aggregation="sum",
    )
    average = AnalysisOrchestrator._select_value(
        rows,
        metric_field="sessions",
        dimension_field="date",
        dimension_kind="date",
        metric_aggregation="sum",
        summary_statistic=SummaryStatistic.AVERAGE,
    )
    latest = AnalysisOrchestrator._select_value(
        rows,
        metric_field="sessions",
        dimension_field="date",
        dimension_kind="date",
        metric_aggregation="sum",
        summary_statistic=SummaryStatistic.LATEST,
    )

    assert total == (Decimal("30"), None, CalculationKind.PERIOD_TOTAL)
    assert average == (Decimal("15"), None, CalculationKind.PERIOD_AVERAGE)
    assert latest == (Decimal("20"), "20260802", CalculationKind.LATEST_VALUE)


@pytest.mark.asyncio
async def test_orchestrator_reuses_cache_and_visualizes_without_mcp(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository,
        [active_users_plan(), active_users_plan(), active_users_plan(visualize=True)],
        ga4_comparison_payload(),
    )

    first = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のUUを前月と比較して",
        request_id="req_first",
    )
    second = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="もう一度同じ条件で",
        request_id="req_second",
    )
    chart = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今の結果をグラフにして",
        request_id="req_chart",
    )

    assert first.cache_hit is False
    assert first.findings[0].value == "120"
    assert first.findings[0].comparison_value == "100"
    assert first.findings[0].change_rate == "0.2"
    assert "前月比 20%" in first.assistant_message.content
    assert first.findings[0].comparison_kind is ComparisonKind.MOM
    calculations = {item.kind: item for item in first.findings[0].calculations}
    assert calculations[CalculationKind.DIFFERENCE].value == "20"
    assert calculations[CalculationKind.RATIO].value == "1.2"
    assert calculations[CalculationKind.CHANGE_RATE].value == "0.2"
    assert second.cache_hit is True
    assert chart.cache_hit is True
    assert chart.visualization is not None
    assert chart.visualization.type.value == "kpi"
    assert len(executor.invocations) == 1
    invocation = executor.invocations[0]
    assert invocation.arguments["property_id"] == "222222222"
    assert invocation.arguments["limit"] == 100
    assert (
        invocation.arguments["dimension_filter"]["and_group"]["expressions"][1][
            "filter"
        ]["field_name"]
        == "streamId"
    )
    assert len(repository.messages) == 6
    assert len(repository.evidence) == 1
    assert len(repository.findings) == 2
    assert repository.sessions["as_orchestrator"].revision == 3
    assert any(
        event.details.get("cache_hit") is True for event in repository.audit_events
    )


@pytest.mark.asyncio
async def test_multi_metric_snapshot_persists_each_finding_and_series(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    sessions = SemanticDefinition(
        id="web-sessions",
        canonical_name="sessions",
        aliases=("セッション",),
        kind="metric",
        aggregation="sum",
        scope=SemanticScope.DATA_SOURCE_SET,
        company_id="company_a",
        data_source_set_id="company_a_web",
        ga4_mapping={"metric": "sessions"},
        version="a-web-v1",
    )
    repository.semantic_definitions[sessions.id] = sessions
    orchestrator, executor = build_orchestrator(
        repository,
        [
            IntentPlan(
                intent="KPI snapshot",
                period={"start": date(2026, 8, 1), "end": date(2026, 8, 31)},
                metric_terms=("UU", "セッション"),
            ),
            IntentPlan(intent="visualize", visualization_only=True),
        ],
        {
            "metric_headers": [
                {"name": "activeUsers"},
                {"name": "sessions"},
            ],
            "rows": [
                {
                    "metric_values": [
                        {"value": "120"},
                        {"value": "180"},
                    ]
                }
            ],
        },
    )

    response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のUUとセッションを見せて",
        request_id="req_multi_metric",
    )
    restored_chart = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今の結果をグラフにして",
        request_id="req_multi_metric_chart",
    )

    assert len(executor.invocations) == 1
    assert [item.value for item in response.findings] == ["120", "180"]
    assert response.visualization is not None
    assert [item.field for item in response.visualization.series] == [
        "activeUsers",
        "sessions",
    ]
    assert set(response.assistant_message.finding_ids) == set(repository.findings)
    assert len(restored_chart.findings) == 2
    assert restored_chart.visualization is not None
    assert len(restored_chart.visualization.series) == 2


@pytest.mark.asyncio
async def test_orchestrator_never_reuses_daily_cache_for_monthly_grain(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    daily = IntentPlan(
        intent="daily active users",
        period={"start": date(2026, 1, 1), "end": date(2026, 8, 31)},
        metric_terms=("UU",),
        time_grain=TimeGrain.DAY,
    )
    monthly = IntentPlan(
        intent="monthly active users",
        time_grain=TimeGrain.MONTH,
    )
    orchestrator, executor = build_orchestrator(
        repository,
        [daily, monthly, monthly],
        {
            "rows": [
                {
                    "date": "20260801",
                    "yearMonth": "202608",
                    "activeUsers": 120,
                }
            ]
        },
    )

    daily_response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="日別でアクティブユーザー数を出して",
        request_id="req_daily",
    )
    monthly_response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="月別でアクティブユーザー数を出して",
        request_id="req_monthly",
    )
    repeated_monthly = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="同じ月別でもう一度",
        request_id="req_monthly_repeated",
    )

    assert daily_response.cache_hit is False
    assert monthly_response.cache_hit is False
    assert repeated_monthly.cache_hit is True
    assert monthly_response.session.state.focus.dimension == "year_month"
    assert [item.arguments["dimensions"] for item in executor.invocations] == [
        ["date"],
        ["yearMonth"],
    ]


@pytest.mark.asyncio
async def test_vague_evaluation_persists_period_then_comprehensive_runs_once(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    for canonical_name, alias, field, aggregation in (
        ("sessions", "セッション", "sessions", "sum"),
        ("engagement_rate", "エンゲージメント率", "engagementRate", None),
        ("key_events", "キーイベント", "keyEvents", "sum"),
    ):
        definition = SemanticDefinition(
            id=f"web-{canonical_name}",
            canonical_name=canonical_name,
            aliases=(alias,),
            kind="metric",
            aggregation=aggregation,
            scope=SemanticScope.DATA_SOURCE_SET,
            company_id="company_a",
            data_source_set_id="company_a_web",
            ga4_mapping={"metric": field},
            version="a-web-v1",
        )
        repository.semantic_definitions[definition.id] = definition
    period = {"start": date(2026, 8, 1), "end": date(2026, 8, 31)}
    orchestrator, executor = build_orchestrator(
        repository,
        [
            IntentPlan(
                intent="vague evaluation",
                period=period,
                metric_terms=("UU",),
            ),
            IntentPlan(
                intent="model chose a metric",
                comparison=ComparisonIntent.PREVIOUS_MONTH,
                metric_terms=("UU",),
            ),
        ],
        {
            "dimension_headers": [{"name": "dateRange"}],
            "metric_headers": [
                {"name": "activeUsers"},
                {"name": "sessions"},
                {"name": "engagementRate"},
                {"name": "keyEvents"},
            ],
            "rows": [
                {
                    "dimension_values": [{"value": "current"}],
                    "metric_values": [
                        {"value": "120"},
                        {"value": "180"},
                        {"value": "0.6"},
                        {"value": "12"},
                    ],
                },
                {
                    "dimension_values": [{"value": "comparison"}],
                    "metric_values": [
                        {"value": "100"},
                        {"value": "160"},
                        {"value": "0.5"},
                        {"value": "10"},
                    ],
                },
            ],
        },
    )

    clarification = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="8月のサイトを評価して",
        request_id="req_clarification",
    )

    assert executor.invocations == []
    assert clarification.session.state.period is not None
    assert clarification.session.state.period.start == date(2026, 8, 1)
    assert "集客" in clarification.assistant_message.content
    assert "エンゲージメント" in clarification.assistant_message.content
    assert "コンバージョン" in clarification.assistant_message.content
    assert "総合レポート" in clarification.assistant_message.content
    assert "前月比較" in clarification.assistant_message.content

    report = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="総合レポート、前月比較",
        request_id="req_site_health",
    )

    assert len(executor.invocations) == 1
    assert executor.invocations[0].arguments["metrics"] == [
        "activeUsers",
        "sessions",
        "engagementRate",
        "keyEvents",
    ]
    assert executor.invocations[0].arguments["dimensions"] == []
    assert len(report.findings) == 4
    assert report.evidence[0].skill_id == AnalysisSkillId.SITE_HEALTH_REPORT.value
    assert repository.sessions["as_orchestrator"].revision == 2
    assert len(repository.messages) == 4


@pytest.mark.asyncio
async def test_unavailable_conversion_returns_guidance_without_mcp(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository,
        [
            IntentPlan(
                intent="conversions",
                period={"start": date(2026, 8, 1), "end": date(2026, 8, 24)},
                metric_terms=("CV",),
            )
        ],
        ga4_comparison_payload(),
    )

    response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のCVは？",
        request_id="req_unavailable",
    )

    assert executor.invocations == []
    assert "CVが定義されていません" in response.assistant_message.content
    assert "UU" in response.assistant_message.content
    assert response.evidence == ()
    assert response.findings == ()
    assert repository.sessions["as_orchestrator"].revision == 1


@pytest.mark.asyncio
async def test_answer_composer_only_frames_trusted_finding_text(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator, _ = build_orchestrator(
        repository,
        [active_users_plan()],
        ga4_comparison_payload(),
        answer_composer=StaticAnswerComposer(),
    )

    response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のUUを前月と比較して",
        request_id="req_composed",
    )

    assert response.assistant_message.content.startswith("利用状況の変化を確認しました")
    assert "- active_users: 120（前月比 20%）" in (response.assistant_message.content)
    composition_audit = next(
        event
        for event in repository.audit_events
        if event.action == "analysis.answer.compose"
    )
    assert composition_audit.status == "success"
    assert composition_audit.details["llm_model"] == "answer-model-test"


@pytest.mark.asyncio
async def test_invalid_answer_draft_falls_back_to_deterministic_finding(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator, _ = build_orchestrator(
        repository,
        [active_users_plan()],
        ga4_comparison_payload(),
        answer_composer=InvalidAnswerComposer(),
    )

    response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のUUを前月と比較して",
        request_id="req_composer_fallback",
    )

    assert response.assistant_message.content == "active_users: 120（前月比 20%）"
    composition_audit = next(
        event
        for event in repository.audit_events
        if event.action == "analysis.answer.compose"
    )
    assert composition_audit.status == "error"
    assert composition_audit.details["error_code"] == "ValueError"


@pytest.mark.asyncio
async def test_answer_composition_timeout_falls_back_and_completes_analysis(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository,
        [active_users_plan()],
        ga4_comparison_payload(),
        answer_composer=HangingAnswerComposer(),
        answer_composition_timeout_seconds=0.001,
    )

    response = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のUUを前月と比較して",
        request_id="req_composer_timeout",
    )

    assert response.assistant_message.content == "active_users: 120（前月比 20%）"
    assert len(executor.invocations) == 1
    assert len(response.rows) == 2
    assert len(response.evidence) == 1
    assert response.visualization is not None
    composition_audit = next(
        event
        for event in repository.audit_events
        if event.action == "analysis.answer.compose"
    )
    assert composition_audit.status == "error"
    assert composition_audit.details["error_code"] == "answer_composition_timeout"
    assert repository.audit_events[-1].action == "analysis.message.complete"
    assert repository.audit_events[-1].status == "success"


@pytest.mark.asyncio
async def test_outer_cancellation_is_not_swallowed_during_answer_composition(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    composer = HangingAnswerComposer()
    orchestrator, _ = build_orchestrator(
        repository,
        [active_users_plan()],
        ga4_comparison_payload(),
        answer_composer=composer,
        answer_composition_timeout_seconds=60,
    )
    task = asyncio.create_task(
        orchestrator.analyze(
            user_id="user-a",
            session_id="as_orchestrator",
            content="今月のUUを前月と比較して",
            request_id="req_composer_cancelled",
        )
    )
    await composer.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not any(
        event.action == "analysis.answer.compose" for event in repository.audit_events
    )
    assert repository.audit_events[-1].details["error_code"] == "analysis_cancelled"


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_never_executes_twice(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository,
        [active_users_plan()],
        ga4_comparison_payload(),
    )

    await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="今月のUU",
        request_id="req_original",
        idempotency_key="test-aaaa",
    )
    with pytest.raises(
        AnalysisOrchestrationError,
        match="analysis_request_duplicate",
    ):
        await orchestrator.analyze(
            user_id="user-a",
            session_id="as_orchestrator",
            content="別の質問へ変更",
            request_id="req_retry",
            idempotency_key="test-aaaa",
        )

    assert len(executor.invocations) == 1
    assert len(repository.messages) == 2
    assert repository.audit_events[-1].details["error_code"] == (
        "analysis_request_duplicate"
    )


@pytest.mark.asyncio
async def test_analysis_timeout_is_typed_and_audited(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    orchestrator = AnalysisOrchestrator(
        repository,
        planner=SlowPlanner(),
        gateway=McpGateway(Settings(environment="test"), repository),
        executor=RecordingExecutor(ga4_comparison_payload()),
        cache=DatasetCache(InMemoryCacheMetadata(), InMemoryCacheBlobs()),
        timeout_seconds=0.001,
    )

    with pytest.raises(AnalysisOrchestrationError, match="analysis_timeout"):
        await orchestrator.analyze(
            user_id="user-a",
            session_id="as_orchestrator",
            content="今月のUU",
            request_id="req_timeout",
        )

    assert repository.audit_events[-1].details["error_code"] == "analysis_timeout"


@pytest.mark.asyncio
async def test_cancelled_analysis_is_audited_without_running_mcp(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    executor = RecordingExecutor(ga4_comparison_payload())
    orchestrator = AnalysisOrchestrator(
        repository,
        planner=SlowPlanner(),
        gateway=McpGateway(Settings(environment="test"), repository),
        executor=executor,
        cache=DatasetCache(InMemoryCacheMetadata(), InMemoryCacheBlobs()),
    )
    task = asyncio.create_task(
        orchestrator.analyze(
            user_id="user-a",
            session_id="as_orchestrator",
            content="今月のUU",
            request_id="req_cancelled",
        )
    )
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert executor.invocations == []
    assert repository.audit_events[-1].details["error_code"] == ("analysis_cancelled")


@pytest.mark.asyncio
async def test_conversation_sequence_preserves_filters_and_reuses_current_result(
    repository: InMemoryRepository,
) -> None:
    await add_session(repository)
    plans = [
        active_users_plan(),
        IntentPlan(intent="organic only", channel_term="Organic"),
        IntentPlan(intent="mobile only", device_term="モバイル"),
        IntentPlan(
            intent="compare previous month",
            comparison=ComparisonIntent.PREVIOUS_MONTH,
        ),
        active_users_plan(visualize=True),
    ]
    orchestrator, executor = build_orchestrator(
        repository,
        plans,
        ga4_comparison_payload(),
    )

    for index, question in enumerate(
        (
            "今月のUUを前月と比較して",
            "Organicだけ",
            "モバイルだけ",
            "前月比較",
            "グラフにして",
        )
    ):
        response = await orchestrator.analyze(
            user_id="user-a",
            session_id="as_orchestrator",
            content=question,
            request_id=f"req_sequence_{index}",
        )

    assert response.session.state.focus.metric == "active_users"
    assert response.session.state.focus.channel == "Organic Search"
    assert response.session.state.focus.device == "mobile"
    assert response.session.state.comparison_period is not None
    assert response.visualization is not None
    assert len(executor.invocations) == 4
    mobile_filter = executor.invocations[-1].arguments["dimension_filter"]
    assert "Organic Search" in str(mobile_filter)
    assert "mobile" in str(mobile_filter)
    assert "3333333333" in str(mobile_filter)


def test_message_endpoint_runs_orchestrator_and_restores_history(
    repository: InMemoryRepository,
) -> None:
    orchestrator, executor = build_orchestrator(
        repository,
        [active_users_plan(), active_users_plan(visualize=True)],
        ga4_comparison_payload(),
    )
    app = create_app(
        Settings(environment="test", enable_analytics_tools=True),
        repository=repository,
        token_verifier=StaticVerifier(),
        orchestrator=orchestrator,
    )
    api = TestClient(app)
    headers = {"Authorization": "Bearer user-a"}
    created = api.post(
        "/api/sessions",
        headers=headers,
        json={"data_source_set_id": "company_a_web"},
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    response = api.post(
        f"/api/sessions/{session_id}/messages",
        headers={**headers, "Idempotency-Key": "test-aaaa"},
        json={"content": "今月のUUを前月と比較して"},
    )
    chart_response = api.post(
        f"/api/sessions/{session_id}/messages",
        headers={**headers, "Idempotency-Key": "test-bbbb"},
        json={"content": "今の結果をグラフにして"},
    )
    duplicate = api.post(
        f"/api/sessions/{session_id}/messages",
        headers={**headers, "Idempotency-Key": "test-aaaa"},
        json={"content": "内容を変えて再送"},
    )
    invalid_key = api.post(
        f"/api/sessions/{session_id}/messages",
        headers={**headers, "Idempotency-Key": "bad key"},
        json={"content": "無効なキー"},
    )
    history = api.get(
        f"/api/sessions/{session_id}/messages",
        headers=headers,
    )
    restored = api.get(
        f"/api/sessions/{session_id}/history",
        headers=headers,
    )

    assert response.status_code == 200
    assert chart_response.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "analysis_request_duplicate"
    assert invalid_key.status_code == 400
    assert invalid_key.json()["detail"]["code"] == "invalid_idempotency_key"
    assert response.json()["findings"][0]["value"] == "120"
    assert response.json()["session"]["revision"] == 1
    assert response.json()["session"]["title"] == ("A社 Web｜今月のUUを前月と比較して")
    assert [message["role"] for message in history.json()] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message["role"] for message in restored.json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    restored_results = restored.json()["results"]
    assert len(restored_results) == 2
    restored_result = restored.json()["latest_result"]
    assert restored_result["visualization"] == chart_response.json()["visualization"]
    assert restored_result["rows"] == chart_response.json()["rows"]
    assert restored_result["evidence"] == chart_response.json()["evidence"]
    assert len(executor.invocations) == 1


def test_message_stream_emits_typed_event_sequence(
    repository: InMemoryRepository,
) -> None:
    orchestrator, _executor = build_orchestrator(
        repository,
        [active_users_plan()],
        ga4_comparison_payload(),
    )
    app = create_app(
        Settings(environment="test", enable_analytics_tools=True),
        repository=repository,
        token_verifier=StaticVerifier(),
        orchestrator=orchestrator,
    )
    api = TestClient(app)
    headers = {"Authorization": "Bearer user-a"}
    session_id = api.post(
        "/api/sessions",
        headers=headers,
        json={"data_source_set_id": "company_a_web"},
    ).json()["id"]

    with api.stream(
        "POST",
        f"/api/sessions/{session_id}/messages/stream",
        headers=headers,
        json={"content": "今月のUUを前月と比較して"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    event_names = [
        line.removeprefix("event: ")
        for line in body.splitlines()
        if line.startswith("event: ")
    ]
    assert event_names == [
        "status",
        "finding",
        "visualization",
        "evidence",
        "done",
    ]
    assert '"cache_hit":false' in body
