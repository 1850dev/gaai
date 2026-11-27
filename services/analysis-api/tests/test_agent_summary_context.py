import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from analytics_ai.agent.conversation import existing_data_only
from analytics_ai.agent.director import VertexAgentDirector
from analytics_ai.agent.models import AgentSummary
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.agent.summary_context import observed_labels, summary_context
from tests.test_agent import Director
from tests.test_orchestrator import add_session, build_orchestrator


@pytest.mark.parametrize(
    "question",
    [
        "新しいデータ取得はせず、前回の5ページを説明してください。",
        "追加取得はしないで評価して。",
        "再取得不要です。",
        "APIを呼ばず説明して。",
        "GA4を呼び出さないで。",
        "Don't fetch new data; explain the previous result.",
    ],
)
def test_explicit_no_fetch_is_an_execution_constraint(question):
    assert existing_data_only(question)


@pytest.mark.parametrize(
    "question",
    [
        "2026年5月の上位5ページを取得。レポートは作成しないで。",
        "広告予算を増やさず流入を分析して。",
        "追加調査をしてください。",
    ],
)
def test_no_fetch_does_not_match_unrelated_business_constraints(question):
    assert not existing_data_only(question)


@pytest.mark.asyncio
async def test_no_fetch_without_prior_data_does_not_call_planner_or_executor(
    repository,
):
    session = await add_session(repository)
    orchestrator, executor = build_orchestrator(repository, [], {"rows": []})
    director = Director([])
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            operation_id="existing-only",
            content="新しいデータ取得はせず、前回の結果を説明してください。",
        )
    ]
    assert events[-1]["event"] == "done"
    assert not executor.invocations
    assert len(director.contexts) == 1  # summary, no planning/execution capability
    assert director.contexts[0]["facts"] == []


def sample_facts():
    return [
        {
            "id": "fact-a",
            "value": "1234",
            "category_values": [
                {"dimensions": {"pagePath": "/campaign/2026/"}, "value": "1234"},
                {"dimensions": {"sessionSource": "1234"}, "value": "100"},
            ],
            "filters": [{"field_name": "sessionSource", "value": "source2.example"}],
        }
    ]


def test_observed_labels_do_not_authorize_measurements_or_unknown_paths():
    labels = observed_labels(sample_facts())
    assert labels == ("/campaign/2026/", "source2.example")
    assert AgentSummary.model_validate(
        {"explanation": "/campaign/2026/ と source2.example の傾向です。"},
        context={"observed_labels": labels},
    )
    for prose in (
        "PVは1234です",
        "12%改善します",
        "/campaign/2027/ を改善します",
        "/campaign/2026/ のPVは100です",
        "5ページを分析しました",
    ):
        with pytest.raises(ValidationError):
            AgentSummary.model_validate(
                {"explanation": prose}, context={"observed_labels": labels}
            )


def test_summary_projection_keeps_evidence_and_context_without_duplicates():
    facts = sample_facts()
    source = {
        "facts": facts,
        "working_memory": {"entries": ["objective"]},
        "historical_analyses": [
            {
                "facts": facts,
                "section": {"period": "May", "observations": ["duplicate"]},
            }
        ],
        "staged_analysis": {
            "phase": "complete",
            "rank_plan": {"duplicate": True},
            "items": [
                {
                    "value": "/page",
                    "status": "complete",
                    "rows": ["duplicate"],
                    "evidence_ids": ["e"],
                }
            ],
        },
    }
    projected = summary_context(source)
    assert projected["facts"] == facts
    assert projected["working_memory"] == source["working_memory"]
    assert "duplicate" not in json.dumps(projected)
    assert "duplicate" in json.dumps(source)  # projection is non-mutating
    assert projected["historical_analyses"][0]["finding_ids"] == ["fact-a"]


@pytest.mark.asyncio
async def test_summary_accepts_verified_numeric_paths_and_sets_thinking_budget():
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        if "issues" in kwargs["config"].response_json_schema["properties"]:
            from tests.test_grounding_review import review_pass

            return SimpleNamespace(text=json.dumps(review_pass()))
        return SimpleNamespace(
            text=json.dumps(
                {
                    "explanation": "取得済みの結果を評価します。",
                    "assessments": [
                        {
                            "interpretation": "/campaign/2026/ は改善候補です。",
                            "fact_ids": ["fact-a"],
                        }
                    ],
                }
            )
        )

    director = object.__new__(VertexAgentDirector)
    director.model = "gemini-2.5-flash"
    director.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )
    summary = await director.summarize({"facts": sample_facts()})
    assert "/campaign/2026/" in summary.assessments[0].interpretation
    assert len(calls) == 2
    assert calls[0]["config"].thinking_config.thinking_budget == 1024
