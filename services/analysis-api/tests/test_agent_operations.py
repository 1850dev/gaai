"""Replay, restart and uncertain execution must never repeat GA4 retrieval."""

from datetime import timedelta

import pytest

from analytics_ai.agent.models import AgentDecision, AgentWorkspace
from analytics_ai.agent.operations import Operation
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import utc_now
from analytics_ai.orchestrator.service import AnalysisOrchestrationError
from tests.test_agent import Director, finish
from tests.test_orchestrator import (
    active_users_plan,
    add_session,
    build_orchestrator,
    ga4_comparison_payload,
)


async def execute(agent, session, content, operation):
    return [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content=content,
            operation_id=operation,
        )
    ]


@pytest.mark.asyncio
async def test_old_operation_replay_has_immutable_response_without_queries(repository):
    session = await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="利用状況",
                explanation="確認します。",
                plan=active_users_plan(),
            ),
            finish(),
            AgentDecision(
                action="ask",
                objective="改善相談",
                explanation="目的を確認します。",
                questions=("対象を教えてください。",),
            ),
        ]
    )
    store = MemoryAgentStore()
    agent = AgentService(repository, store, director, orchestrator)
    first = await execute(agent, session, "先月の利用状況を分析して", "old-operation")
    await execute(agent, session, "別の改善を相談したい", "new-operation")
    before = (
        len(executor.invocations),
        len(director.contexts),
        len(repository.messages),
    )
    replay = await execute(
        AgentService(repository, store, director, orchestrator),
        session,
        "先月の利用状況を分析して",
        "old-operation",
    )
    assert replay == [first[-1]]
    assert before == (
        len(executor.invocations),
        len(director.contexts),
        len(repository.messages),
    )
    with pytest.raises(AnalysisOrchestrationError, match="operation_payload_conflict"):
        await execute(agent, session, "異なる質問", "old-operation")
    grant = repository.grants[("user-a", session.data_source_set_id)]
    repository.grants[("user-a", session.data_source_set_id)] = grant.model_copy(
        update={"enabled": False}
    )
    with pytest.raises(AccessDenied):
        await execute(agent, session, "先月の利用状況を分析して", "old-operation")


@pytest.mark.asyncio
async def test_restart_after_answer_commit_recovers_candidate_without_generation(
    repository, monkeypatch
):
    session = await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    director = Director(
        [
            AgentDecision(
                action="ask",
                objective="目的確認",
                explanation="教えてください。",
                questions=("どのページですか？",),
            )
        ]
    )
    store = MemoryAgentStore()
    agent = AgentService(repository, store, director, orchestrator)
    original = agent._save_workspace

    async def crash(session, expected, updated):
        if updated.status == "waiting":
            raise RuntimeError("simulated crash after answer commit")
        return await original(session, expected, updated)

    monkeypatch.setattr(agent, "_save_workspace", crash)
    events = await execute(agent, session, "改善したい", "crash-operation")
    assert events[-1]["event"] == "error"
    assert any(m.role.value == "assistant" for m in repository.messages.values())
    recovered = await AgentService(repository, store, director, orchestrator).operation(
        "user-a", session.id, "crash-operation"
    )
    assert (
        recovered["status"] == "complete" and recovered["result"]["status"] == "waiting"
    )
    assert (
        not executor.invocations and len(director.contexts) == 1
    )  # asking for clarification needs planning only
    assert (await agent.workspace("user-a", session.id)).status == "waiting"


@pytest.mark.asyncio
async def test_expired_lease_marks_interrupted_without_external_retry(repository):
    session = await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    store = MemoryAgentStore()
    agent = AgentService(repository, store, Director([]), orchestrator)
    source = repository.source_sets[session.data_source_set_id]
    import hashlib

    from analytics_ai.agent.operations import request_hash

    value = Operation(
        session_id=session.id,
        operation_id="expired-operation",
        request_hash=request_hash("分析して", False, None),
        source_hash=hashlib.sha256(source.model_dump_json().encode()).hexdigest(),
        semantic_version=session.state.semantic_version,
        status="running",
        lease_until=utc_now() - timedelta(seconds=1),
    )
    await agent.operations.save("user-a", source.id, value, None)
    replay = await execute(agent, session, "分析して", "expired-operation")
    assert replay == [{"event": "error", "data": {"code": "agent_interrupted"}}]
    assert not executor.invocations
    assert (await agent.operation("user-a", session.id, "expired-operation"))[
        "status"
    ] == "interrupted"


@pytest.mark.asyncio
async def test_prepared_without_committed_answer_is_not_released(repository):
    session = await add_session(repository)
    orchestrator, _ = build_orchestrator(repository, [], ga4_comparison_payload())
    agent = AgentService(repository, MemoryAgentStore(), Director([]), orchestrator)
    source = repository.source_sets[session.data_source_set_id]
    import hashlib

    value = Operation(
        session_id=session.id,
        operation_id="prepared-operation",
        request_hash="hash",
        source_hash=hashlib.sha256(source.model_dump_json().encode()).hexdigest(),
        semantic_version=session.state.semantic_version,
        status="prepared",
        lease_until=utc_now() - timedelta(seconds=1),
        workspace_json=AgentWorkspace(status="complete").model_dump_json(),
    )
    await agent.operations.save("user-a", source.id, value, None)
    saved = await agent.operation("user-a", session.id, "prepared-operation")
    assert saved["status"] == "interrupted" and saved["result"] is None
