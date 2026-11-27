from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from analytics_ai.agent.director import generation_schema
from analytics_ai.agent.models import (
    AgentDecision,
    AgentSummary,
    GroundedAssessment,
    Proposal,
    ReportSchedule,
    SiteContext,
)
from analytics_ai.agent.schedules import ScheduleService, next_run, reporting_period
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.config import Settings
from analytics_ai.domain.models import DatePeriod, utc_now
from analytics_ai.orchestrator.service import AnalysisOrchestrationError
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_api_authorization import auth, client
from tests.test_orchestrator import (
    active_users_plan,
    add_session,
    build_orchestrator,
    ga4_comparison_payload,
)


class Director:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = list(decisions)
        self.contexts: list[dict[str, Any]] = []

    async def decide(self, context: dict[str, Any]) -> AgentDecision:
        self.contexts.append(context)
        return self.decisions.pop(0)

    async def summarize(self, context: dict[str, Any]) -> AgentSummary:
        self.contexts.append(context)
        return AgentSummary(
            explanation="取得したデータを評価しました。",
            assessments=(
                GroundedAssessment(
                    interpretation="利用状況を確認しました。",
                    fact_ids=tuple(f["id"] for f in context["facts"][:8]),
                ),
            )
            if context["facts"]
            else (),
            questions=("次にチャネル別を確認しますか？",),
        )


def finish() -> AgentDecision:
    return AgentDecision(
        action="finish",
        objective="集客改善レポート",
        explanation="利用状況を確認しました。次は改善仮説を検証します。",
    )


def test_generation_schema_retains_properties_and_local_validation() -> None:
    schema = generation_schema()
    proposal_schema = schema["$defs"]["Proposal"]
    assert "title" in proposal_schema["properties"]
    assert set(proposal_schema["required"]).issubset(proposal_schema["properties"])
    assert "maxLength" not in proposal_schema["properties"]["title"]
    with pytest.raises(ValueError):
        AgentDecision(
            action="ask",
            objective="目的",
            explanation="12%改善します",
            questions=("相談",),
        )


@pytest.mark.asyncio
async def test_agent_sse_api_authorizes_and_restores_saved_report(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="レポート",
                explanation="状況を確認します。",
                plan=active_users_plan(),
            ),
            finish(),
        ]
    )
    app = client(repository)
    app.app.state.services = replace(
        app.app.state.services,
        settings=Settings(environment="test", enable_analytics_tools=True),
        agent=AgentService(repository, MemoryAgentStore(), director, orchestrator),
    )
    path = f"/api/sessions/{session.id}/agent/stream"
    assert (
        app.post(path, headers=auth("user-b"), json={"content": "分析して"}).status_code
        == 403
    )
    response = app.post(
        path,
        headers={**auth("user-a"), "Idempotency-Key": "agent_api_test"},
        json={"content": "分析して"},
    )
    assert response.status_code == 200
    assert "event: analysis" in response.text
    assert "event: done" in response.text
    assert len(executor.invocations) == 1
    restored = app.get(
        f"/api/sessions/{session.id}/agent", headers=auth("user-a")
    ).json()
    assert restored["sections"][0]["facts"]


@pytest.mark.asyncio
async def test_conversation_asks_then_queries_then_restores_report(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    director = Director(
        [
            AgentDecision(
                action="ask",
                objective="集客改善レポート",
                explanation="改善の対象を確認させてください。",
                questions=("どの期間を評価しますか？",),
            ),
            AgentDecision(
                action="query",
                objective="集客改善レポート",
                explanation="指定された期間の状況を確認します。",
                plan=active_users_plan(),
            ),
            finish(),
        ]
    )
    store = MemoryAgentStore()
    agent = AgentService(repository, store, director, orchestrator)
    await agent.save_site(
        "user-a",
        session.data_source_set_id,
        SiteContext(
            site_basics="情報サイト", technical_constraints="広告予算を増やさない"
        ),
    )
    first = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="サイト改善を相談したい",
            operation_id="turn_ask",
        )
    ]
    assert first[-1]["data"]["status"] == "waiting"
    assert not executor.invocations
    second = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="先月を確認して",
            operation_id="turn_query",
        )
    ]
    assert second[-1]["event"] == "done"
    assert len(executor.invocations) == 1
    assert director.contexts[1]["site_context"]["site_basics"] == "情報サイト"
    assert director.contexts[1]["workspace"]["questions"] == [
        "どの期間を評価しますか？"
    ]
    assert director.contexts[2]["facts"][0]["evidence_ids"]
    restored = await AgentService(repository, store, director, orchestrator).workspace(
        "user-a", session.id
    )
    assert restored.sections[0].finding_ids
    assert restored.context_revision == 1
    before = len(repository.messages)
    replay = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="先月を確認して",
            operation_id="turn_query",
        )
    ]
    assert replay[-1]["data"]["revision"] == restored.revision
    assert len(repository.messages) == before
    assert len(executor.invocations) == 1


def proposal(**updates: Any) -> Proposal:
    return Proposal(
        title="記事導線の改善",
        hypothesis="回遊を促せる可能性がある",
        changes="関連記事への導線を設ける",
        surfaces=("content",),
        validation_plan="変更前後と対照ページを比較する",
        success_metric="active_users",
        evidence_ids=("evidence_ok",),
        **updates,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_surfaces": ("home",)},
        {"allow_new_content": False},
    ],
)
def test_site_interests_do_not_block_grounded_proposals(change: dict[str, Any]) -> None:
    decision = finish().model_copy(
        update={"proposals": (proposal(content_action="new"),)}
    )
    AgentService.validate_decision(
        decision,
        SiteContext(**change),
        [{"evidence_ids": ["evidence_ok"]}],
        {"active_users"},
    )


def test_proposal_cannot_invent_evidence_or_success_metric() -> None:
    decision = finish().model_copy(update={"proposals": (proposal(),)})
    with pytest.raises(ValueError, match="evidence_or_metric"):
        AgentService.validate_decision(decision, SiteContext(), [], {"active_users"})
    with pytest.raises(ValueError, match="evidence_or_metric"):
        AgentService.validate_decision(
            decision, SiteContext(), [{"evidence_ids": ["evidence_ok"]}], {"sessions"}
        )


def test_site_and_preferences_api_scope_revision_and_gate(
    repository: InMemoryRepository,
) -> None:
    app = client(repository)
    path = "/api/data-source-sets/company_a_web/site-context"
    assert app.get(path).status_code == 401
    assert app.get(path, headers=auth("user-b")).status_code == 403
    saved = app.put(path, json={"site_basics": "情報サイト"}, headers=auth("user-a"))
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert (
        app.put(path, json={"site_basics": "stale"}, headers=auth("user-a")).status_code
        == 409
    )
    prefs = app.put(
        "/api/settings/properties",
        json={"hidden_property_ids": ["222222222"]},
        headers=auth("user-a"),
    )
    assert prefs.status_code == 200
    assert (
        app.get("/api/settings/properties", headers=auth("user-b")).json()[
            "hidden_property_ids"
        ]
        == []
    )
    assert (
        app.get("/api/data-source-sets", headers=auth("user-a")).json()[0]["id"]
        == "company_a_web"
    )
    scheduled = app.post(
        "/api/schedules",
        json={
            "data_source_set_id": "company_a_web",
            "name": "weekly",
            "prompt": "分析して",
            "enabled": True,
        },
        headers=auth("user-a"),
    )
    assert scheduled.status_code == 503


@pytest.mark.asyncio
async def test_same_company_cannot_access_report_or_site(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    agent = AgentService(repository, MemoryAgentStore(), None, None)
    with pytest.raises(AccessDenied):
        await agent.workspace("user-b", session.id)
    with pytest.raises(AccessDenied):
        await agent.save_site("user-b", session.data_source_set_id, SiteContext())


def test_calendar_and_completed_reporting_periods() -> None:
    weekly = ReportSchedule(
        data_source_set_id="web", name="weekly", prompt="report", hour=9
    )
    instant = datetime(2026, 9, 7, 0, tzinfo=UTC)
    assert next_run(weekly, instant) == datetime(2026, 9, 14, 0, tzinfo=UTC)
    assert reporting_period(weekly, instant) == DatePeriod(
        start=date(2026, 8, 31), end=date(2026, 9, 6)
    )
    monthly = weekly.model_copy(update={"frequency": "monthly"})
    assert reporting_period(monthly, datetime(2024, 3, 1, tzinfo=UTC)).end == date(
        2024, 2, 29
    )
    assert next_run(monthly, datetime(2026, 12, 31, tzinfo=UTC)) == datetime(
        2027, 1, 1, 0, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_scheduler_atomic_claim_deduplicates_and_reauthorizes(
    repository: InMemoryRepository,
) -> None:
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    director = Director([finish()])
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    scheduler = ScheduleService(agent)
    then = datetime(2026, 9, 6, tzinfo=UTC)
    now = datetime(2026, 9, 7, 1, tzinfo=UTC)
    value = ReportSchedule(
        data_source_set_id="company_a_web",
        name="weekly",
        prompt="まとめて",
        enabled=True,
    )
    saved = await scheduler.save("user-a", value, now=then)
    counts = await asyncio.gather(scheduler.run_due(now), scheduler.run_due(now))
    assert sum(counts) == 1
    assert len(repository.sessions) == 1
    assert not executor.invocations
    assert director.contexts[0]["scheduled_period"] == {
        "start": "2026-08-31",
        "end": "2026-09-06",
    }
    latest = (await scheduler.list("user-a"))[0]
    assert latest.last_status == "complete"
    assert latest.id == saved.id
    repository.grants.clear()
    await scheduler.run_due(now + timedelta(days=7))
    latest = (await scheduler.list("user-a"))[0]
    assert latest.last_error == "access_denied"
    assert len(repository.sessions) == 1
    stopped = await scheduler.save(
        "user-a", latest.model_copy(update={"enabled": False})
    )
    assert not stopped.enabled
    assert stopped.next_run_at is None


@pytest.mark.asyncio
async def test_running_turn_rejects_competitor_and_revoked_grant(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    orchestrator, _ = build_orchestrator(repository, [], ga4_comparison_payload())
    agent = AgentService(
        repository, MemoryAgentStore(), Director([finish()]), orchestrator
    )
    workspace = await agent.workspace("user-a", session.id)
    await agent.checkpoint(
        session,
        workspace,
        status="running",
        lease_until=utc_now() + timedelta(seconds=100),
    )
    with pytest.raises(AnalysisOrchestrationError, match="agent_turn_in_progress"):
        _ = [
            e
            async for e in agent.turn(
                user_id="user-a",
                session_id=session.id,
                content="続けて",
                operation_id="second",
            )
        ]
    repository.grants.clear()
    with pytest.raises(AccessDenied):
        _ = [
            e
            async for e in agent.turn(
                user_id="user-a",
                session_id=session.id,
                content="続けて",
                operation_id="second",
            )
        ]
