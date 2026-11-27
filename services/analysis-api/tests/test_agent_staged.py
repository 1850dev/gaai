from datetime import date

import pytest

from analytics_ai.agent.models import AgentDecision
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.staged import create_staged, is_resume, record_result
from analytics_ai.agent.staged_models import GroupedRequest
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.domain.models import DatePeriod
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.orchestrator.planning import IntentPlan
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director
from tests.test_cross_breakdown import register
from tests.test_orchestrator import add_session, build_orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary,secondary,limit,question",
    [
        ("page_path", "session_source", 5, "上位対象ごとの内訳と改善を調査"),
        ("country", "device_category", 5, "上位対象ごとの内訳と改善を調査"),
        ("page_path", "session_source", 10, "上位対象ごとの内訳と改善を調査"),
        (
            "page_path",
            "session_source",
            5,
            "分析の目的は、広告予算を増やさず、既存ページの改善につながる仮説を見つけることです。\n\n"
            "2026年5月のページビュー数（PV）上位5ページを最初に確定してください。\n"
            "その5ページそれぞれについて、参照元別のPV上位5件を調べてください。\n\n"
            "全体の「ページ×参照元」の上位5組合せではなく、先に選んだ各ページの内訳を調べてください。\n"
            "期間比較は不要です。レポートはまだ作成しないでください。",
        ),
    ],
)
async def test_rank_then_each_entity_completes_without_another_user_message(
    repository: InMemoryRepository,
    primary: str,
    secondary: str,
    limit: int,
    question: str,
) -> None:
    session = await add_session(repository)
    fields = register(repository)
    base = IntentPlan(
        intent="上位対象と各内訳",
        metric_terms=("page_views",),
        dimension_terms=(primary, secondary),
        period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="段階調査",
                explanation="対象を確定します。",
                plan=base,
                grouped_request=GroupedRequest(
                    primary=primary, secondary=secondary, limit=limit
                ),
            )
        ]
    )
    orchestrator, executor = build_orchestrator(repository, [], {"rows": []})
    calls = []

    async def execute(invocation):
        calls.append(invocation)
        axes = invocation.arguments["dimensions"]
        if axes == [fields[primary]]:
            rows = [
                {fields[primary]: f"entity-{i}", "screenPageViews": str(100 - i)}
                for i in range(limit)
            ]
        else:
            assert axes == [fields[secondary]]
            assert "entity-" in str(invocation.arguments["dimension_filter"])
            rows = [
                {fields[secondary]: "google", "screenPageViews": "50"},
                {fields[secondary]: "direct", "screenPageViews": "20"},
            ]
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )

    executor.execute = execute
    store = MemoryAgentStore()
    agent = AgentService(repository, store, director, orchestrator)
    first = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content=question,
            operation_id="staged-1",
        )
    ]
    assert first[-1]["event"] == "done", first[-1]
    workspace = await agent.workspace("user-a", session.id)
    assert workspace.status == "complete"
    assert len(calls) == limit + 1  # rank + ALL children, not a global joint top N
    assert sum(i.status == "complete" for i in workspace.staged_plan.items) == limit
    assert workspace.staged_plan.phase == "complete"
    assert "『続き』" not in workspace.turns[-1]["content"]
    assert "最大4回" not in workspace.turns[-1]["content"]
    # After a process restart, explanations must not query or replan at all.
    agent = AgentService(repository, store, Director([]), orchestrator)
    second = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content=(
                f"新しいデータ取得はせず、前回までに取得した{limit}ページの分析結果を使って説明してください。\n"
                "最初に伝えた分析目的と制約を確認したうえで、ページごとの参照元の違いと、"
                "そこから考えられる改善仮説を整理してください。\n"
                "確認できた事実と仮説を分け、根拠となるページ・参照元・数値を示してください。\n"
                "PVだけでは判断できないことも明記してください。\nレポートはまだ作成しないでください。"
            )
            if primary == "page_path"
            else (
                "新しいデータ取得はせず、前回の国ごとの端末内訳をPVで説明してください。レポートは不要です。"
            ),
            operation_id="staged-2",
        )
    ]
    assert second[-1]["event"] == "done", second[-1]
    workspace = await agent.workspace("user-a", session.id)
    assert workspace.staged_plan.phase == "complete"
    assert len(calls) == limit + 1
    assert workspace.status == "complete"
    assert workspace.completed_queries == 0
    assert not workspace.report_requested
    assert len(agent.director.contexts[-1]["facts"]) == limit + 1
    assert agent.director.contexts[-1]["data_acquisition_allowed"] is False
    assert all(i.status == "complete" for i in workspace.staged_plan.items)
    state = (await agent.session("user-a", session.id)).state
    assert not state.filters  # last child's filter must not become thread scope
    assert state.period == base.period


def test_resume_does_not_silently_ignore_changed_conditions() -> None:
    assert is_resume("続きをお願いします")
    assert not is_resume("続きを2026年6月のモバイルだけで")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "revocation", "research_timeout"])
async def test_automatic_plan_keeps_timeout_and_authorization_boundaries(
    repository, failure, monkeypatch
):
    clock = [0.0]
    monkeypatch.setattr("analytics_ai.agent.service.monotonic", lambda: clock[0])
    session = await add_session(repository)
    fields = register(repository)
    plan = IntentPlan(
        intent="上位ページと各参照元",
        metric_terms=("page_views",),
        period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="ページ改善",
                explanation="確認します",
                plan=plan,
                grouped_request=GroupedRequest(
                    primary="page_path", secondary="session_source", limit=5
                ),
            )
        ]
    )
    orchestrator, executor = build_orchestrator(repository, [], {"rows": []})
    calls = []

    async def execute(invocation):
        calls.append(invocation)
        if len(calls) == 5:
            if failure == "timeout":
                raise TimeoutError
            if failure == "revocation":
                repository.grants.clear()
            if failure == "research_timeout":
                clock[0] = 241.0
        rows = (
            [
                {fields["page_path"]: f"/page-{i}", "screenPageViews": 100 - i}
                for i in range(5)
            ]
            if len(calls) == 1
            else [{fields["session_source"]: "google", "screenPageViews": 10}]
        )
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )

    executor.execute = execute
    store = MemoryAgentStore()
    agent = AgentService(repository, store, director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="上位対象と各内訳を調査",
            operation_id="bounded-auto",
        )
    ]
    assert len(calls) == 5  # goes past four, but never past revocation / timeout
    if failure == "revocation":
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "access_denied"
        return
    assert events[-1]["data"]["stop_reason"] == (
        "tool_failed" if failure == "timeout" else "application_time_budget"
    )
    if failure == "timeout":
        assert "analysis_timeout" in events[-1]["data"]["execution_errors"]
    workspace = await agent.workspace("user-a", session.id)
    pending = 2 if failure == "timeout" else 1
    assert sum(i.status == "pending" for i in workspace.staged_plan.items) == pending
    assert "『続き』" not in workspace.turns[-1]["content"]
    # A no-fetch follow-up may describe partial evidence, never fill missing data.
    agent = AgentService(repository, store, Director([]), orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="新しいデータ取得はせず前回の5ページを説明してください",
            operation_id="partial-discussion",
        )
    ]
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "partial"
    assert len(calls) == 5
    assert f"未完了の内訳: {pending}対象" in events[-1]["data"]["turns"][-1]["content"]


@pytest.mark.asyncio
async def test_staged_rejects_ambiguous_rows_and_comparison(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    register(repository)
    definitions = list(repository.semantic_definitions.values())
    base = IntentPlan(intent="上位ページ", metric_terms=("page_views",))
    request = GroupedRequest(primary="page_path", secondary="session_source", limit=10)
    staged = create_staged(request, base, session.state, definitions, "調査")
    with pytest.raises(ValueError, match="ambiguous"):
        record_result(
            staged,
            [{"pagePathPlusQueryString": "/a", "screenPageViews": "1"}] * 2,
            session.state,
            ("e",),
        )
    comparison = DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))
    with pytest.raises(ValueError, match="single_period"):
        create_staged(
            request,
            base.model_copy(update={"comparison_period": comparison}),
            session.state,
            definitions,
            "比較",
        )
