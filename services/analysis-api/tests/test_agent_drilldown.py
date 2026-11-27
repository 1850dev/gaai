from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from analytics_ai.agent.models import AgentDecision, ObservedFilter, ReportSchedule
from analytics_ai.agent.schedules import ScheduleService
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.domain.models import SemanticDefinition, SemanticScope
from analytics_ai.orchestrator.planning import ComparisonIntent
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director, finish
from tests.test_orchestrator import (
    active_users_plan,
    add_session,
    build_orchestrator,
    ga4_comparison_payload,
)


@pytest.mark.asyncio
async def test_observed_page_drilldown_inherits_and_can_clear_filters(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    definition = SemanticDefinition(
        id="page",
        canonical_name="page_path",
        aliases=("ページ",),
        kind="dimension",
        scope=SemanticScope.GLOBAL,
        version="a-web-v1",
        ga4_mapping={"dimension": "pagePath"},
    )
    repository.semantic_definitions[definition.id] = definition
    payload = {
        "dimension_headers": [{"name": "pagePath"}],
        "metric_headers": [{"name": "activeUsers", "type": "TYPE_INTEGER"}],
        "rows": [
            {
                "dimension_values": [{"value": "/articles"}],
                "metric_values": [{"value": "120"}],
            }
        ],
        "row_count": 1,
    }
    plan = active_users_plan().model_copy(
        update={"dimension_terms": ("page_path",), "comparison": ComparisonIntent.NONE}
    )

    def query(intent: str, **extra: Any) -> AgentDecision:
        return AgentDecision(
            action="query",
            objective="記事改善",
            explanation="ページ別の状況を確認します。",
            plan=plan.model_copy(update={"intent": intent, **extra}),
        )

    director = Director(
        [
            query("ページ比較"),
            query("記事を深掘り").model_copy(
                update={
                    "observed_filters": (
                        ObservedFilter(field="pagePath", value="/articles"),
                    )
                }
            ),
            finish(),
            query("引き継いでデバイス限定", device_term="mobile"),
            finish(),
            query("絞り込みを解除", clear_filters=True),
            finish(),
        ]
    )
    orchestrator, executor = build_orchestrator(repository, [], payload)
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    for op in ("first", "second", "third"):
        events = [
            e
            async for e in agent.turn(
                user_id="user-a",
                session_id=session.id,
                content="調査を進めて",
                operation_id=op,
            )
        ]
        assert events[-1]["event"] == "done", events[-1]
        state = (await agent.session("user-a", session.id)).state
        if op == "first":
            assert state.filters[0].value == "/articles"
            assert (
                director.contexts[1]["workspace"]["sections"][0]["observations"][0][
                    "pagePath"
                ]
                == "/articles"
            )
        if op == "second":
            assert {f.value for f in state.filters} == {"/articles", "mobile"}
        if op == "third":
            assert not state.filters
            assert state.focus.device is None
    assert len(executor.invocations) == 3  # Cleared query reuses the original cache.


@pytest.mark.asyncio
async def test_unobserved_filter_rejected_before_execution(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    bad = AgentDecision(
        action="query",
        objective="調査",
        explanation="調べます。",
        plan=active_users_plan(),
        observed_filters=(ObservedFilter(field="pagePath", value="/invented"),),
    )
    director = Director([bad, finish()])
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    _ = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="調べて",
            operation_id="invalid_filter",
        )
    ]
    assert not executor.invocations
    assert (
        director.contexts[1]["validation_error"]
        == "filter_value_not_in_authorized_observations"
    )


@pytest.mark.asyncio
async def test_agent_query_budget_replay_uses_ledger_not_mutable_workspace(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    decisions = [
        AgentDecision(
            action="query",
            objective="調査",
            explanation="追加調査を行います。",
            plan=active_users_plan().model_copy(update={"intent": f"analysis_{i}"}),
        )
        for i in range(7)
    ]
    director = Director(decisions)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="詳細レポート",
            operation_id="budget",
        )
    ]
    assert events[-1]["data"]["status"] == "partial"
    assert events[-1]["data"]["completed_queries"] == 1
    assert len(executor.invocations) == 1
    workspace = await agent.workspace("user-a", session.id)
    await agent.checkpoint(
        session, workspace, status="failed", last_error="agent_interrupted"
    )
    replay = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="詳細レポート",
            operation_id="budget",
        )
    ]
    assert replay == [events[-1]]
    assert len(executor.invocations) == 1


@pytest.mark.asyncio
async def test_worker_recovers_expired_claim_without_reexecution(
    repository: InMemoryRepository,
) -> None:
    director = Director([])
    orchestrator, _ = build_orchestrator(repository, [], ga4_comparison_payload())
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    scheduler = ScheduleService(agent)
    now = datetime(2026, 9, 7, 1, tzinfo=UTC)
    saved = await scheduler.save(
        "user-a",
        ReportSchedule(
            data_source_set_id="company_a_web",
            name="weekly",
            prompt="report",
            enabled=True,
        ),
        now=now,
    )
    crashed = saved.model_copy(
        update={
            "revision": saved.revision + 1,
            "last_status": "running",
            "running_until": now - timedelta(seconds=1),
        }
    )
    assert await agent.store.save(scheduler.record("user-a", crashed), saved.revision)
    assert await scheduler.run_due(now) == 0
    latest = (await scheduler.list("user-a"))[0]
    assert latest.last_error == "worker_interrupted_no_automatic_replay"
    assert latest.running_until is None
    assert not repository.sessions
