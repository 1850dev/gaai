from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from analytics_ai.agent.director import (
    PlannerPhaseTimeout,
    PlannerPhaseValidationError,
    VertexAgentDirector,
    ga4_argument_context,
)
from analytics_ai.config import Settings


def selection(tool: str = "ga4.analyze") -> dict[str, Any]:
    return {
        "tool": tool,
        "workflow_id": "analysis.exploration.v1",
        "objective": "参照元別の深掘り",
        "explanation": "保存済みの条件を引き継いで確認します。",
        "analysis_brief": "参照元別にランディングページを確認する",
        "remaining_tasks": [],
        "questions": ["対象のページを指定できますか？"] if tool == "user.ask" else [],
    }


def query(**plan: Any) -> dict[str, Any]:
    return {
        "plan": {
            "intent": "参照元ごとのランディングページを調べる",
            "metric_terms": ["sessions"],
            "dimension_terms": ["landing_page", "session_source"],
            **plan,
        },
        "observed_filters": [],
    }


def vertex(
    responses: list[dict[str, Any]], *, grounding: bool = False
) -> tuple[VertexAgentDirector, list[Any]]:
    calls: list[Any] = []

    async def generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(responses.pop(0), ensure_ascii=False))

    director = object.__new__(VertexAgentDirector)
    director.model = "synthetic-model"
    director.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )
    # Legacy unit tests isolate planning/schema contracts. Grounding has its own
    # SDK-level tests with grounding=True; production never bypasses the review.
    if not grounding:
        director._review_grounding = AsyncMock(return_value=None)
    return director, calls


@pytest.mark.asyncio
async def test_historical_citations_cannot_skip_requested_grouped_plan() -> None:
    director, calls = vertex(
        [
            selection("evidence.answer"),
            query(
                metric_terms=["page_views"],
                period={"start": "2026-05-01", "end": "2026-05-31"},
            ),
        ]
    )
    result = await director.decide(
        {
            "user_question": "上位5ページを確定して各ページの参照元を調べて",
            "analysis_state": {},
            "historical_analyses": [{"historical": True}],
            "required_grouped_request": {
                "primary": "page_path",
                "secondary": "session_source",
                "limit": 5,
            },
            "query_budget": 4,
            "data_acquisition_allowed": True,
        }
    )
    assert result.action == "query"
    assert result.grouped_request and result.grouped_request.limit == 5
    assert result.workflow_id == "analysis.grouped_breakdown.v1"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,allowed,budget",
    [
        ("user.ask", True, 4),
        ("evidence.answer", False, 4),
        ("evidence.answer", True, 0),
    ],
)
async def test_grouped_request_keeps_ask_no_fetch_and_budget_boundaries(
    tool, allowed, budget
):
    director, calls = vertex([selection(tool)])
    result = await director.decide(
        {
            "user_question": "過去の上位ページを説明して",
            "analysis_state": {},
            "required_grouped_request": {
                "primary": "page_path",
                "secondary": "session_source",
                "limit": 5,
            },
            "query_budget": budget,
            "data_acquisition_allowed": allowed,
        }
    )
    assert result.action == ("ask" if tool == "user.ask" else "finish")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("omit", [False, True])
async def test_followup_materializes_only_omitted_metrics_from_server_state(
    omit: bool,
) -> None:
    arguments = query(metric_terms=[])
    if omit:
        del arguments["plan"]["metric_terms"]
    director, calls = vertex([selection(), arguments])
    state = {
        "metrics": ["sessions", "active_users"],
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
    }
    result = await director.decide(
        {
            "user_question": "その参照元をLP別で深掘りしてください",
            "analysis_state": state,
        }
    )
    assert len(calls) == 2
    assert result.plan
    assert result.plan.metric_terms == ("sessions", "active_users")
    assert result.plan.dimension_terms == ("landing_page", "session_source")
    assert arguments["plan"].get("metric_terms", []) == []
    assert state["metrics"] == ["sessions", "active_users"]


@pytest.mark.asyncio
async def test_explicit_metrics_are_never_replaced_by_inherited_metrics() -> None:
    director, _ = vertex([selection(), query(metric_terms=["page_views"])])
    result = await director.decide(
        {"user_question": "PVで見て", "analysis_state": {"metrics": ["sessions"]}}
    )
    assert result.plan and result.plan.metric_terms == ("page_views",)


@pytest.mark.asyncio
async def test_missing_initial_metrics_fails_without_guessing_or_query_execution() -> (
    None
):
    director, calls = vertex(
        [selection(), query(metric_terms=[]), query(metric_terms=[])]
    )
    with pytest.raises(PlannerPhaseValidationError) as caught:
        await director.decide({"user_question": "調べて", "analysis_state": {}})
    assert caught.value.phase == "ga4_arguments"
    assert (
        "ga4_query_requires_explicit_or_inherited_metric_terms" in caught.value.feedback
    )
    assert (
        len(calls) == 3
    )  # selection, arguments, arguments repair; never a new selection


@pytest.mark.asyncio
async def test_argument_repair_preserves_selected_action() -> None:
    director, calls = vertex(
        [
            selection(),
            query(
                comparison="previous_month",
                comparison_period={"start": "2026-05-01", "end": "2026-05-31"},
            ),
            query(comparison_period={"start": "2026-05-01", "end": "2026-05-31"}),
        ]
    )
    result = await director.decide(
        {"user_question": "参照元をLP別で深掘りしてください"}
    )
    assert result.plan and result.plan.comparison_period
    assert len(calls) == 3
    first_arguments = json.loads(calls[1]["contents"])
    repaired_arguments = json.loads(calls[2]["contents"])
    assert first_arguments["selected_action"] == repaired_arguments["selected_action"]
    assert "explicit_and_relative_comparison" in repaired_arguments["validation_error"]
    assert "IntentPlan" not in calls[0]["config"].response_json_schema.get("$defs", {})
    assert "IntentPlan" in calls[2]["config"].response_json_schema["$defs"]


@pytest.mark.asyncio
async def test_failed_argument_diagnostics_do_not_expose_provider_values() -> None:
    director, calls = vertex(
        [
            selection(),
            query(property_id="secret-response-value"),
            query(property_id="x"),
        ]
    )
    with pytest.raises(PlannerPhaseValidationError) as caught:
        await director.decide({"user_question": "深掘りしてください"})
    assert caught.value.phase == "ga4_arguments"
    assert "property_id" in caught.value.feedback
    assert "secret-response-value" not in caught.value.feedback
    assert "secret-response-value" not in calls[2]["contents"]
    assert all("input" not in issue for issue in json.loads(caught.value.feedback))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["selection", "ga4_arguments"])
async def test_timeout_identifies_model_phase_not_research_or_ga4_quota(
    phase: str,
) -> None:
    director, calls = vertex([])
    director.phase_timeouts = {"selection": 0.03, "ga4_arguments": 0.03, "advice": 0.03}

    async def generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if phase == "ga4_arguments" and len(calls) == 1:
            return SimpleNamespace(text=json.dumps(selection()))
        await asyncio.sleep(1)
        raise AssertionError("model call must be cancelled at phase deadline")

    director.client.aio.models.generate_content = generate
    with pytest.raises(PlannerPhaseTimeout) as caught:
        await director.decide({"user_question": "もっと深掘りして"})
    assert caught.value.phase == phase
    assert len(calls) == (1 if phase == "selection" else 2)


@pytest.mark.asyncio
async def test_schema_repair_shares_phase_deadline_instead_of_resetting_timeout() -> (
    None
):
    director, calls = vertex([])
    director.phase_timeouts = {"selection": 1, "ga4_arguments": 0.09, "advice": 1}

    async def generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(selection()))
        await asyncio.sleep(0.06)
        return SimpleNamespace(text=json.dumps(query(metric_terms=[])))

    director.client.aio.models.generate_content = generate
    with pytest.raises(PlannerPhaseTimeout) as caught:
        await director.decide({"user_question": "深掘りして"})
    assert caught.value.phase == "ga4_arguments"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_selection_repairs_nonqualitative_conversation_without_ga4_call() -> None:
    malformed = {**selection("user.ask"), "explanation": "2026年の期間を確認します。"}
    director, calls = vertex([malformed, selection("user.ask")])
    result = await director.decide({"user_question": "その結果について相談したい"})
    assert result.action == "ask" and result.plan is None
    assert len(calls) == 2
    assert (
        "use_qualitative_prose" in json.loads(calls[1]["contents"])["validation_error"]
    )


@pytest.mark.asyncio
async def test_advice_cannot_switch_to_query_and_repairs_locally() -> None:
    action = {
        **selection("evidence.answer"),
        "workflow_id": "advisory.site_improvement.v1",
    }
    common = {"objective": "改善案を相談", "explanation": "改善の仮説を確認します。"}
    director, calls = vertex(
        [
            action,
            {**common, "action": "query", "plan": query()["plan"]},
            {**common, "action": "finish"},
        ]
    )
    result = await director.decide({"user_question": "改善の方向性を相談したい"})
    assert result.action == "finish" and result.plan is None
    assert len(calls) == 3
    assert "literal_error" in json.loads(calls[2]["contents"])["validation_error"]


def test_argument_context_is_bounded_and_omits_unneeded_agent_memory() -> None:
    selected_action = selection()
    projection = ga4_argument_context(
        {
            "user_question": "そのLPについて深掘りして",
            "selected_action": selected_action,
            "analysis_state": {
                "metrics": ["sessions"],
                "findings": [{"text": "unneeded facts"}],
                "dataset_ids": ["old-dataset"],
            },
            "facts": [{"text": "unneeded facts"}],
            "workspace": {
                "proposals": [{"description": "old proposal"}],
                "execution_steps": [{"plan": "old plan"}],
                "turns": [{"role": "assistant", "content": "文" * 4000}] * 20,
                "sections": [
                    {
                        "dimension_fields": ["landingPage"],
                        "observations": [{"landingPage": "/precise/path"}] * 30,
                        "facts": ["old fact"],
                    }
                ]
                * 24,
            },
        }
    )
    assert projection["user_question"] == "そのLPについて深掘りして"
    assert projection["selected_action"] == selected_action
    assert projection["analysis_state"] == {"metrics": ["sessions"]}
    assert "facts" not in projection
    workspace = projection["workspace"]
    assert "proposals" not in workspace and "execution_steps" not in workspace
    assert len(workspace["turns"]) == 4
    assert len(workspace["turns"][-1]["content"]) == 1500
    assert len(workspace["sections"]) == 8
    assert len(workspace["sections"][0]["observations"]) == 12
    assert workspace["sections"][0]["observations"][0] == {
        "landingPage": "/precise/path"
    }


def test_sdk_retry_is_disabled_so_application_is_the_single_retry_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: dict[str, Any] = {}

    def client(**kwargs: Any) -> Any:
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("analytics_ai.agent.director.genai.Client", client)
    VertexAgentDirector(Settings(environment="test"))
    assert options["http_options"].retry_options.attempts == 1
    assert options["http_options"].timeout == 65_000
