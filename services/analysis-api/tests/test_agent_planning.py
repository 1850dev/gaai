from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from analytics_ai.agent.director import (
    Ga4ToolCall,
    VertexAgentDirector,
    ga4_argument_schema,
    generation_schema,
)
from analytics_ai.agent.models import AgentDecision
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.agent.workflows import ToolSelection
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director
from tests.test_orchestrator import (
    active_users_plan,
    add_session,
    build_orchestrator,
    ga4_comparison_payload,
)

QUESTION = "2026年5月と2026年6月を比較した流入元の分析をしてください"


def selection(tool: str = "ga4.analyze") -> dict[str, Any]:
    return {
        "tool": tool,
        "workflow_id": "analysis.acquisition_comparison.v1",
        "objective": "流入元の期間比較",
        "explanation": "流入元を比較します。",
        "analysis_brief": "期間全体を確認し、次に参照元とメディア別に比較する",
        "remaining_tasks": ["期間全体", "流入元別"],
        "questions": ["未定義の指標の定義を確認できますか？"]
        if tool == "user.ask"
        else [],
    }


def vertex(responses: list[dict[str, Any]]) -> tuple[VertexAgentDirector, list[Any]]:
    calls: list[Any] = []

    async def generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(responses.pop(0), ensure_ascii=False))

    director = object.__new__(VertexAgentDirector)
    director.model = "synthetic-model"
    director.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )
    return director, calls


def test_action_and_ga4_argument_grammars_are_separate() -> None:
    schema = generation_schema(ToolSelection)
    assert "IntentPlan" not in schema.get("$defs", {})
    assert "Proposal" not in schema.get("$defs", {})
    query = ga4_argument_schema()["$defs"]["IntentPlan"]
    assert "sql" not in query["properties"]
    assert {"period", "comparison_period", "metric_terms"}.issubset(query["required"])
    with pytest.raises(ValidationError):
        Ga4ToolCall.model_validate(
            {
                "plan": {"intent": "範囲偽装", "property_id": "123"},
                "observed_filters": [],
            }
        )
    with pytest.raises(ValidationError, match="ga4_query_requires"):
        Ga4ToolCall(plan={"intent": "空の調査"}, observed_filters=())


@pytest.mark.asyncio
async def test_two_stage_query_preserves_source_and_normalizes_exact_months() -> None:
    director, calls = vertex(
        [
            selection(),
            {
                "plan": {
                    "intent": "流入元別の比較",
                    "metric_terms": ["sessions", "active_users"],
                    "dimension_terms": ["session_source", "session_medium"],
                    "period": {"start": "2026-06-01", "end": "2026-06-30"},
                    "comparison": "previous_month",
                },
                "observed_filters": [],
            },
        ]
    )
    result = await director.decide({"user_question": QUESTION})
    assert len(calls) == 2
    assert result.action == "query" and result.plan
    assert result.plan.comparison_period
    assert result.plan.comparison_period.end.isoformat() == "2026-05-31"
    assert result.plan.period and result.plan.period.end.isoformat() == "2026-06-30"
    assert result.plan.dimension_terms == ("session_source", "session_medium")
    assert result.plan.metric_terms == ("sessions", "active_users")
    assert result.workflow_id == "analysis.acquisition_comparison.v1"
    assert result.assumptions and "日数" in result.assumptions[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,action", [("user.ask", "ask"), ("evidence.answer", "finish")]
)
async def test_conversation_actions_never_generate_ga4_arguments(
    tool: str, action: str
) -> None:
    director, calls = vertex([selection(tool)])
    result = await director.decide({"user_question": QUESTION})
    assert len(calls) == 1
    assert result.action == action and result.plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,attempts", [(429, 2), (503, 2), (400, 1), (403, 1)])
async def test_provider_retry_is_bounded_and_never_retries_permission_errors(
    code: int,
    attempts: int,
) -> None:
    from google.genai.errors import ClientError

    director, _ = vertex([])
    calls = 0

    async def failing(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ClientError(code, {"error": {"message": "synthetic error"}})

    director.client.aio.models.generate_content = failing
    with pytest.raises(ClientError):
        await director.decide({"user_question": QUESTION})
    assert calls == attempts


class FailingDirector(Director):
    async def decide(self, context: dict[str, Any]) -> AgentDecision:
        if self.decisions:
            return await super().decide(context)
        # A provider error must not be described as a manual stop or GA4 quota.
        raise RuntimeError("synthetic provider failure; must not be logged")


@pytest.mark.asyncio
@pytest.mark.parametrize("after_query", [False, True])
async def test_planner_failure_answers_from_verified_evidence_and_trace(
    repository: InMemoryRepository, after_query: bool, caplog: pytest.LogCaptureFixture
) -> None:
    await add_session(repository)
    orchestrator, executor = build_orchestrator(
        repository, [], ga4_comparison_payload()
    )
    director = FailingDirector(
        [
            AgentDecision(
                action="query",
                objective="流入比較",
                explanation="期間全体を確認します。",
                plan=active_users_plan(),
                workflow_id="analysis.acquisition_comparison.v1",
                remaining_tasks=("流入元別",),
            )
        ]
        if after_query
        else []
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        event
        async for event in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content=QUESTION,
            operation_id="provider-failure",
        )
    ]
    assert events[-1]["event"] == "done", events[-1]
    workspace = events[-1]["data"]
    assert workspace["status"] == "partial"
    assert workspace["stop_reason"] == "planning_failed"
    assert workspace["execution_errors"] == ["agent_planner_unavailable"]
    assert len(executor.invocations) == int(after_query)
    answer = (await repository.list_messages("as_orchestrator"))[-1]
    assert "計画サービス" in answer.content
    assert "調査を停止しました" not in answer.content
    assert "synthetic provider failure" not in caplog.text
    if after_query:
        assert answer.evidence_ids
        assert workspace["execution_steps"][0]["status"] == "succeeded"
        assert workspace["execution_steps"][0]["evidence_ids"]
        restored = await agent.workspace("user-a", "as_orchestrator")
        assert restored.execution_steps[0].status == "succeeded"


@pytest.mark.asyncio
async def test_explicit_breakdown_is_not_replaced_by_general_review(
    repository: InMemoryRepository,
) -> None:
    from tests.test_agent import finish
    from tests.test_agent_review import configure_review

    agent, director, executor = await configure_review(repository)
    plan = active_users_plan().model_copy(
        update={
            "dimension_terms": ("session_channel_group",),
            "skill_id": None,
        }
    )
    director.decisions = [
        AgentDecision(
            action="query",
            objective="流入チャネルを多角的に比較",
            explanation="比較します。",
            plan=plan,
        ),
        finish(),
    ]
    events = [
        event
        async for event in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content="チャネルごとに多角的に比較して",
            operation_id="specific-axis",
        )
    ]
    assert events[-1]["event"] == "done"
    assert len(executor.invocations) == 1
    assert executor.invocations[0].arguments["dimensions"] == [
        "sessionDefaultChannelGroup"
    ]


@pytest.mark.asyncio
async def test_acquisition_cannot_finish_before_explicit_source_axis(
    repository: InMemoryRepository,
) -> None:
    from analytics_ai.domain.models import SemanticDefinition, SemanticScope
    from tests.test_agent import finish
    from tests.test_agent_review import configure_review

    agent, director, executor = await configure_review(repository)
    definition = SemanticDefinition(
        id="source",
        canonical_name="session_source",
        aliases=("流入元",),
        kind="dimension",
        scope=SemanticScope.GLOBAL,
        version="a-web-v1",
        ga4_mapping={"dimension": "sessionSource"},
    )
    repository.semantic_definitions[definition.id] = definition
    from analytics_ai.domain.models import DatePeriod

    base = active_users_plan().model_copy(
        update={
            "period": DatePeriod(start="2026-06-01", end="2026-06-30"),
        }
    )
    initial = AgentDecision(
        action="query",
        objective="流入元比較",
        explanation="比較します。",
        plan=base,
        workflow_id="analysis.acquisition_comparison.v1",
    )
    director.decisions = [
        initial,
        finish(),
        initial.model_copy(
            update={
                "plan": base.model_copy(update={"dimension_terms": ("session_source",)})
            }
        ),
        finish(),
    ]
    events = [
        event
        async for event in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content=QUESTION,
            operation_id="coverage",
        )
    ]
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "complete"
    assert len(executor.invocations) == 2
    assert executor.invocations[-1].arguments["dimensions"] == ["sessionSource"]
    assert "requested_analysis_not_observed" in director.contexts[2]["validation_error"]
    sections = events[-1]["data"]["sections"]
    assert sections[0]["dimension_fields"] == []
    assert sections[1]["dimension_fields"] == ["sessionSource"]
