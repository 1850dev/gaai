from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from analytics_ai.agent.conversation import planning_workspace, query_context
from analytics_ai.agent.director import PlannerPhaseTimeout, PlannerPhaseValidationError
from analytics_ai.agent.models import AgentDecision, AgentWorkspace
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.domain.models import AnalysisState, FindingRef
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director, finish
from tests.test_orchestrator import active_users_plan, add_session, build_orchestrator


def test_planner_context_does_not_grow_with_persisted_findings_or_long_turns() -> None:
    workspace = AgentWorkspace(
        turns=tuple(
            {"role": "user", "content": "過去の長い回答" * 500} for _ in range(20)
        ),
        questions=("次はページ別で確認しますか？",),
    )
    projected = planning_workspace(workspace, set())
    assert len(projected["turns"]) == 4
    assert all(len(t["content"]) <= 1000 for t in projected["turns"])
    assert projected["history_truncated"]
    assert projected["questions"] == ["次はページ別で確認しますか？"]
    assert len(json.dumps(projected, ensure_ascii=False)) < 6000
    state = AnalysisState(
        data_source_set_id="fixed",
        semantic_version="v1",
        metrics=("active_users",),
        findings=(
            FindingRef(id="old", text="長い数値結果" * 500, evidence_ids=("e1",)),
        ),
        dataset_ids=("d1",),
        router_history=("ga4",) * 100,
    )
    compact = query_context(state)
    assert "findings" not in compact and "router_history" not in compact
    assert compact["metrics"] == ["active_users"]
    assert compact["semantic_version"] == "v1"


class FailingPhaseDirector(Director):
    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self.error = error

    async def decide(self, context: dict[str, Any]) -> AgentDecision:
        self.contexts.append(context)
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "stop", "code", "label"),
    [
        (
            PlannerPhaseTimeout("ga4_arguments"),
            "planning_phase_timeout",
            "agent_ga4_arguments_timeout",
            "GA4分析条件の生成",
        ),
        (
            PlannerPhaseValidationError("selection", "[]"),
            "planning_failed",
            "agent_selection_invalid_schema",
            "次の行動の選択",
        ),
    ],
)
async def test_phase_failure_is_not_misreported_as_full_research_timeout(
    repository: InMemoryRepository, error: Exception, stop: str, code: str, label: str
) -> None:
    session = await add_session(repository)
    director = FailingPhaseDirector(error)
    orchestrator, executor = build_orchestrator(repository, [], {})
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="詳しく教えて",
            operation_id="phase",
        )
    ]
    assert events[-1]["data"]["status"] == "partial"
    assert events[-1]["data"]["stop_reason"] == stop
    assert events[-1]["data"]["execution_errors"] == [code]
    assert not executor.invocations
    assert len([c for c in director.contexts if "query_budget" in c]) == 1
    answer = (await repository.list_messages(session.id))[-1]
    assert label in answer.content
    assert "調査時間（最大240秒）" not in answer.content
    assert events[-1]["data"]["lease_until"] is None


@pytest.mark.asyncio
async def test_repaired_legacy_validation_error_is_audited_but_not_final_failure(
    repository: InMemoryRepository,
) -> None:
    class RecoveringDirector(Director):
        async def decide(self, context: dict[str, Any]) -> AgentDecision:
            if not self.contexts:
                self.contexts.append(context)
                # Protocol-compatible external directors can still raise raw
                # ValidationError; successful correction must clear the UI warning.
                with pytest.raises(ValidationError) as caught:
                    AgentDecision(action="query", objective="test", explanation="確認")
                raise caught.value
            return await super().decide(context)

    session = await add_session(repository)
    director = RecoveringDirector(
        [
            AgentDecision(
                action="query",
                objective="利用状況",
                explanation="確認",
                plan=active_users_plan(),
            ),
            finish(),
        ]
    )
    orchestrator, _ = build_orchestrator(
        repository, [], {"rows": [{"activeUsers": 10}]}
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="利用状況",
            operation_id="repaired",
        )
    ]
    assert events[-1]["data"]["status"] == "complete"
    assert (
        "invalid_schema_or_generated_numbers" in events[-1]["data"]["execution_errors"]
    )
    assert (
        "AIの計画が形式" not in (await repository.list_messages(session.id))[-1].content
    )
