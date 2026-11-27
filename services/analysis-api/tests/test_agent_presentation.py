import hashlib

import pytest

from analytics_ai.agent.models import AgentDecision
from analytics_ai.agent.presentation import attributed_message, report_intent
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.domain.models import AnalysisMessage, MessageRole
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director, finish
from tests.test_orchestrator import (
    active_users_plan,
    add_session,
    build_orchestrator,
    ga4_comparison_payload,
)


def test_all_six_historical_breakdowns_keep_page_scope_and_numbers():
    from analytics_ai.agent.presentation import render_summary

    facts = [
        {
            "id": f"f-{i}",
            "metric": "page_views",
            "label": "ページビュー数",
            "text": "確認済み",
            "evidence_ids": [f"e-{i}"],
            "dimension_fields": ["sessionSource"],
            "historical": True,
            "recorded_at": "2026-09-15T00:00:00Z",
            "period": {"start": "2026-05-01", "end": "2026-05-31"},
            "filters": [
                {"field_name": "pagePathPlusQueryString", "value": f"/page-{i}/"}
            ],
            "category_values": [
                {"dimensions": {"sessionSource": "google"}, "value": str(100 + i)}
            ],
        }
        for i in range(6)
    ]
    body = render_summary(None, facts, (), ())
    for i in range(6):
        assert f"ページ=/page-{i}/" in body
        assert f"| google | {100 + i} |" in body
    assert "未取得" not in body


def test_report_requires_explicit_request() -> None:
    assert report_intent("2026年5月の流入指標を分析してください") is None
    assert report_intent("レポートを作成してください") is True
    assert report_intent("これをレポートにして") is True
    assert report_intent("レポートは不要です") is False
    assert report_intent("レポートはまだ作らなくていい") is False
    assert report_intent("期間比較とレポート作成は不要です。") is False
    assert report_intent("レポートを作成してください。追加取得は不要です。") is True
    assert report_intent("追加取得は不要なのでレポートにまとめてください") is True
    assert report_intent("レポートは作成しないでください") is False


def test_legacy_execution_identified_by_server_id_not_just_text() -> None:
    digest = hashlib.sha256(b"session\0user\0operation:query:0").hexdigest()
    message = AnalysisMessage(
        id=f"msg_{digest[:32]}",
        request_id="operation",
        session_id="session",
        user_id="u",
        company_id="c",
        data_source_set_id="d",
        role=MessageRole.USER,
        content="調査: 流入指標",
    )
    assert attributed_message(message).kind == "agent_execution"
    assert attributed_message(message).role == MessageRole.ASSISTANT
    genuine = message.model_copy(update={"id": "msg_" + "a" * 32})
    assert attributed_message(genuine).role == MessageRole.USER


@pytest.mark.asyncio
async def test_budget_always_summarizes_obtained_data_without_more_queries(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="流入分析",
                explanation="取得します。",
                plan=active_users_plan().model_copy(update={"intent": f"step_{i}"}),
            )
            for i in range(7)
        ]
    )
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="流入指標を分析してください",
            operation_id="summary",
        )
    ]
    workspace = events[-1]["data"]
    assert workspace["status"] == "partial"
    assert not workspace["report_requested"]
    summary_context = director.contexts[-1]
    assert summary_context["facts"]
    assert summary_context["stop_reason"] == "application_step_budget"
    assert workspace["completed_queries"] == 1
    assert (
        "duplicate_query_choose_another_analysis_or_finish"
        in workspace["execution_errors"]
    )
    messages = await repository.list_messages(session.id)
    assert sum(m.role == MessageRole.USER for m in messages) == 1
    assert any(m.kind == "agent_execution" for m in messages)
    assert "次にチャネル別" in messages[-1].content
    assert messages[-1].content_format == "markdown"
    restored = AnalysisMessage.model_validate_json(messages[-1].model_dump_json())
    assert restored.content_format == "markdown"
    assert restored.content == messages[-1].content
    assert "アプリ側" in messages[-1].content
    assert len(executor.invocations) == 1
    director.decisions = [finish()]
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="レポートにまとめてください",
            operation_id="report",
        )
    ]
    assert events[-1]["data"]["report_requested"]
    assert len(executor.invocations) == 1


@pytest.mark.asyncio
async def test_failed_semantic_plans_reach_summary_with_diagnostics(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="分析",
                explanation="確認します。",
                plan=active_users_plan().model_copy(
                    update={
                        "intent": f"invalid_{i}",
                        "metric_terms": ("undefined_metric",),
                    }
                ),
            )
            for i in range(7)
        ]
    )
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="分析して",
            operation_id="failure",
        )
    ]
    assert events[-1]["event"] == "done"
    assert "semantic_term_not_found" in director.contexts[-1]["execution_errors"]
    assert not director.contexts[-1]["facts"]
    assert not executor.invocations
