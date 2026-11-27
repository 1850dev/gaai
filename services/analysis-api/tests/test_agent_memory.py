from datetime import date

import pytest

from analytics_ai.agent.memory import ConversationMemory, remember_user
from analytics_ai.agent.memory_models import (
    AgreementChange,
    MemoryQuote,
    WorkingMemory,
    WorkingMemoryChange,
)
from analytics_ai.agent.models import AgentWorkspace, ReportSection
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore, record_id
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import DatePeriod
from analytics_ai.orchestrator.planning import IntentPlan
from analytics_ai.orchestrator.service import AnalysisOrchestrationError
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director, finish
from tests.test_api_authorization import auth, client
from tests.test_cross_breakdown import register
from tests.test_orchestrator import add_session, build_orchestrator


def test_working_memory_preserves_purpose_and_targets_beyond_recent_chat() -> None:
    memory = remember_user(
        WorkingMemory(),
        "目的は回遊改善。/campaign/spring を対象にする。広告予算増は不可",
        "msg-first",
    )
    for i in range(30):
        memory = remember_user(memory, "分析を続けて", f"msg-{i}")
    assert any(e.kind == "objective" and "回遊改善" in e.text for e in memory.entries)
    assert any(
        e.kind == "target" and e.text == "/campaign/spring" for e in memory.entries
    )
    assert all(e.origin == "user_statement" for e in memory.entries)
    assert not memory.verified_finding_ids


@pytest.mark.asyncio
async def test_agreement_confirmation_edit_delete_and_cas(
    repository: InMemoryRepository,
) -> None:
    store = MemoryAgentStore()
    memory = ConversationMemory(repository, store)
    a = await memory.change_agreement(
        "user-a",
        "company_a_web",
        AgreementChange(action="propose", text="決済は変更不可"),
    )
    assert a.status == "pending"
    confirmed = await memory.change_agreement(
        "user-a",
        "company_a_web",
        AgreementChange(action="confirm", id=a.id, revision=a.revision),
    )
    assert confirmed.status == "confirmed"
    with pytest.raises(AnalysisOrchestrationError, match="agent_revision_conflict"):
        await memory.change_agreement(
            "user-a",
            "company_a_web",
            AgreementChange(action="delete", id=a.id, revision=a.revision),
        )
    edited = await memory.change_agreement(
        "user-a",
        "company_a_web",
        AgreementChange(
            action="edit",
            id=a.id,
            revision=confirmed.revision,
            text="決済画面も変更可能",
        ),
    )
    assert edited.status == "pending"  # edits cannot silently become confirmed
    deleted = await memory.change_agreement(
        "user-a",
        "company_a_web",
        AgreementChange(action="delete", id=a.id, revision=edited.revision),
    )
    assert deleted.text == "" and deleted.status == "deleted"
    assert not await memory.agreements("user-a", "company_a_web")
    record = await store.get("agreements", record_id("user-a", "company_a_web"))
    assert "決済" not in str(record.payload)
    assert all("決済" not in str(a.details) for a in repository.audit_events)
    with pytest.raises(ValueError):
        AgreementChange(action="confirm", id="x", text="変更を紛れ込ませる")
    with pytest.raises(AccessDenied):
        await memory.agreements("user-b", "company_a_web")


@pytest.mark.asyncio
async def test_memory_api_requires_source_grant_and_explicit_confirmation(
    repository: InMemoryRepository,
) -> None:
    app = client(repository)
    path = "/api/data-source-sets/company_a_web/memory/agreements"
    response = app.post(
        path,
        headers=auth("user-a"),
        json={"action": "propose", "text": "広告予算増は除外"},
    )
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["status"] == "pending"
    assert (
        app.post(
            path,
            headers=auth("user-b"),
            json={"action": "confirm", "id": item["id"], "revision": 1},
        ).status_code
        == 403
    )
    assert (
        app.post(
            path,
            headers=auth("user-a"),
            json={"action": "confirm", "id": item["id"], "revision": 1},
        ).json()["status"]
        == "confirmed"
    )
    assert (
        app.get(
            "/api/data-source-sets/company_a_store/memory/search?q=広告",
            headers=auth("user-a"),
        ).status_code
        == 403
    )


@pytest.mark.asyncio
async def test_archived_analysis_retrieved_after_workspace_trim_and_reauthorized(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    register(repository)
    orchestrator, _ = build_orchestrator(
        repository,
        [],
        {
            "rows": [
                {
                    "pagePathPlusQueryString": "/campaign/spring",
                    "screenPageViews": "321",
                }
            ]
        },
    )
    agent = AgentService(
        repository, MemoryAgentStore(), Director([finish()]), orchestrator
    )
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id=session.id,
        content="春ページのPV",
        request_id="history",
        trusted_plan=IntentPlan(
            intent="春ページのPV",
            metric_terms=("page_views",),
            dimension_terms=("page_path",),
            period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
        ),
    )
    section = ReportSection(
        title="春ページのPV",
        message_id=result.assistant_message.id,
        finding_ids=tuple(f.id for f in result.findings),
        evidence_ids=tuple(e.id for e in result.evidence),
        period=result.session.state.period,
        dimension_fields=("pagePathPlusQueryString",),
        metric_fields=("screenPageViews",),
        observations=(
            {"pagePathPlusQueryString": "/campaign/spring", "screenPageViews": "321"},
        ),
    )
    await agent.memory.archive(result.session, "2026年5月 /campaign/spring PV", section)
    # Memory index survives even after workspace contains no sections at all.
    await agent.checkpoint(result.session, AgentWorkspace(), sections=())
    hits = await agent.memory.search(
        "user-a", session.data_source_set_id, "/campaign/spring"
    )
    assert len(hits["hits"]) == 1 and hits["hits"][0]["historical"]
    assert hits["hits"][0]["section"]["period"]["start"] == "2026-05-01"
    with pytest.raises(AccessDenied):
        await agent.memory.search(
            "user-b", session.data_source_set_id, "/campaign/spring"
        )
    # A deleted/missing source session invalidates retrieval, despite index data.
    del repository.sessions[session.id]
    assert not (
        await agent.memory.search(
            "user-a", session.data_source_set_id, "/campaign/spring"
        )
    )["hits"]


@pytest.mark.asyncio
async def test_memory_injected_after_many_turns_pending_agreement_not_policy(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    orchestrator, _ = build_orchestrator(repository, [], {"rows": []})
    director = Director([finish()])
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    memory = remember_user(
        WorkingMemory(), "目的は回遊改善。広告予算増は不可", "old-message"
    )
    await agent.checkpoint(
        session,
        AgentWorkspace(),
        memory=memory,
        turns=tuple({"role": "user", "content": "別の質問"} for _ in range(20)),
    )
    await agent.memory.change_agreement(
        "user-a",
        session.data_source_set_id,
        AgreementChange(action="propose", text="未確認ポリシー"),
    )
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="最初の目的は？",
            operation_id="memory-turn",
        )
    ]
    assert events[-1]["event"] == "done"
    context = director.contexts[0]
    assert "回遊改善" in str(context["working_memory"])
    assert not context["confirmed_agreements"]


@pytest.mark.asyncio
async def test_model_memory_requires_verbatim_user_quote_and_can_be_removed(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    orchestrator, _ = build_orchestrator(repository, [], {"rows": []})
    decision = finish().model_copy(
        update={
            "memory_quotes": (
                MemoryQuote(kind="decision", text="チェックアウトはいじれません"),
                MemoryQuote(kind="decision", text="購入率が改善した"),
            )
        }
    )
    agent = AgentService(
        repository, MemoryAgentStore(), Director([decision]), orchestrator
    )
    _ = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="チェックアウトはいじれません",
            operation_id="quote",
        )
    ]
    workspace = await agent.workspace("user-a", session.id)
    assert any(
        e.kind == "decision" and e.text == "チェックアウトはいじれません"
        for e in workspace.memory.entries
    )
    assert not any("購入率" in e.text for e in workspace.memory.entries)
    removed = await agent.change_working_memory(
        "user-a",
        session.id,
        WorkingMemoryChange(
            revision=workspace.revision,
            action="delete",
            kind="decision",
            text="チェックアウトはいじれません",
        ),
    )
    assert not any(e.kind == "decision" for e in removed.memory.entries)
    with pytest.raises(AccessDenied):
        await agent.change_working_memory(
            "user-b",
            session.id,
            WorkingMemoryChange(
                revision=removed.revision,
                action="add",
                kind="decision",
                text="他ユーザーから上書き",
            ),
        )


@pytest.mark.asyncio
async def test_confirmed_agreement_context_is_owner_only_and_grant_revocation_blocks(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    orchestrator, _ = build_orchestrator(repository, [], {"rows": []})
    agent = AgentService(
        repository, MemoryAgentStore(), Director([finish()]), orchestrator
    )
    item = await agent.memory.change_agreement(
        "user-a",
        session.data_source_set_id,
        AgreementChange(action="propose", text="広告予算は増やさない"),
    )
    await agent.memory.change_agreement(
        "user-a",
        session.data_source_set_id,
        AgreementChange(action="confirm", id=item.id, revision=item.revision),
    )
    _ = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="改善を考えて",
            operation_id="confirmed",
        )
    ]
    assert agent.director.contexts[0]["confirmed_agreements"][0]["text"] == item.text
    grant = await repository.get_user_access_grant("user-a", session.data_source_set_id)
    repository.grants[("user-a", session.data_source_set_id)] = grant.model_copy(
        update={"enabled": False}
    )
    with pytest.raises(AccessDenied):
        await agent.memory.agreements("user-a", session.data_source_set_id)
