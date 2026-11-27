"""Model-family policy and redacted diagnostics for Q2b-1 recovery."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from google.genai.errors import ClientError

from analytics_ai.agent.director import AdvisoryDecision
from analytics_ai.agent.models import AgentSummary
from analytics_ai.agent.workflows import ToolSelection
from analytics_ai.observability.logging import JsonFormatter
from tests.test_agent_continuation import run_turn
from tests.test_agent_summary_context import sample_facts
from tests.test_director_continuation import query, selection, vertex
from tests.test_time_grain_intent import harness


@pytest.mark.parametrize(
    "model,temperature,level,budget",
    [
        ("gemini-3.7-flash", 1.0, "LOW", None),
        ("gemini-3-flash-preview", 1.0, "LOW", None),
        ("gemini-2.5-flash", 0.0, None, 1024),
        ("synthetic-model", 0.0, None, None),
    ],
)
def test_family_policy_keeps_legacy_and_never_mixes_thinking_controls(
    model, temperature, level, budget
):
    director, _ = vertex([])
    director.model = model
    config = director._config(ToolSelection, "instructions")
    assert config.temperature == temperature
    assert getattr(config.thinking_config, "thinking_level", None) == level
    assert getattr(config.thinking_config, "thinking_budget", None) == budget
    assert config.response_json_schema
    assert config.automatic_function_calling.disable
    if model.startswith("gemini-3"):
        # Q3c-4c: leave time for the existing grounding review and one repair.
        assert (
            director._config(AdvisoryDecision, "advice").thinking_config.thinking_level
            == "LOW"
        )


@pytest.mark.asyncio
async def test_gemini_three_argument_and_summary_calls_use_bounded_policy():
    director, calls = vertex(
        [
            selection(),
            query(),
            {
                "explanation": "取得済みの根拠を整理します。",
                "assessments": [
                    {
                        "interpretation": "検証可能な改善仮説です。",
                        "fact_ids": ["fact-a"],
                    }
                ],
            },
        ]
    )
    director.model = "gemini-3.7-flash"
    await director.decide({"user_question": "参照元ごとのページを確認して"})
    summary = await director.summarize({"facts": sample_facts()})
    assert isinstance(summary, AgentSummary)
    assert len(calls) == 3
    for call in calls:
        assert "phase" not in call and "repair_attempt" not in call
        assert call["config"].temperature == 1.0
        assert call["config"].thinking_config.thinking_level == "LOW"
        assert call["config"].thinking_config.thinking_budget is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,attempts", [(400, 1), (403, 1), (429, 2), (503, 2)])
async def test_attempt_diagnostics_do_not_leak_provider_or_prompt_data(
    code, attempts, caplog, monkeypatch
):
    director, calls = vertex([])

    async def fail(**kwargs):
        calls.append(kwargs)
        raise ClientError(code, {"error": {"message": "PRIVATE_PROVIDER_TOKEN"}})

    waits = []

    async def no_wait(delay):
        waits.append(delay)
        return None

    monkeypatch.setattr("analytics_ai.agent.director.asyncio.sleep", no_wait)
    director.client.aio.models.generate_content = fail
    caplog.set_level(logging.INFO)
    with pytest.raises(ClientError):
        await director._content(
            phase="summary", repair_attempt=1, contents="PRIVATE_CUSTOMER_PROMPT"
        )
    records = [r for r in caplog.records if r.message == "Agent model attempt"]
    assert len(calls) == len(records) == attempts
    if code == 429:
        assert len(waits) == 1 and 4.0 <= waits[0] <= 6.0
    elif code == 503:
        assert waits == [0.5]
    else:
        assert not waits
    for index, record in enumerate(records, 1):
        serialized = JsonFormatter().format(record)
        payload = json.loads(serialized)
        assert payload["model_phase"] == "summary"
        assert payload["model_attempt"] == index
        assert payload["repair_attempt"] == 1
        assert payload["provider_status"] == code
        assert payload["model_outcome"] == "provider_error"
        assert payload["latency_ms"] >= 0
        assert "PRIVATE" not in serialized


@pytest.mark.asyncio
async def test_cancelled_call_is_never_retried_or_mislabeled_as_provider_error(caplog):
    director, calls = vertex([])

    async def cancel(**kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError()

    director.client.aio.models.generate_content = cancel
    caplog.set_level(logging.INFO)
    with pytest.raises(asyncio.CancelledError):
        await director._content(phase="selection", contents="PRIVATE_PROMPT")
    assert len(calls) == 1
    payload = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert payload["model_outcome"] == "cancelled_or_deadline"
    assert "provider_status" not in payload
    assert "PRIVATE" not in str(payload)


@pytest.mark.asyncio
async def test_summary_transient_retry_reuses_context_without_analytics(
    caplog, monkeypatch
):
    director, calls = vertex([])
    director.model = "gemini-3.7-flash"

    async def recover(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ClientError(503, {"error": {"message": "PRIVATE_FAILURE"}})
        return SimpleNamespace(
            text=json.dumps(
                {
                    "explanation": "保存済み根拠の説明です。",
                    "assessments": [
                        {"interpretation": "改善の仮説です。", "fact_ids": ["fact-a"]}
                    ],
                }
            )
        )

    async def no_wait(_):
        return None

    monkeypatch.setattr("analytics_ai.agent.director.asyncio.sleep", no_wait)
    director.client.aio.models.generate_content = recover
    caplog.set_level(logging.INFO)
    result = await director.summarize({"facts": sample_facts()})
    assert result.assessments[0].fact_ids == ("fact-a",)
    assert len(calls) == 2
    assert calls[0]["contents"] == calls[1]["contents"]
    assert calls[0]["config"] == calls[1]["config"]
    assert [r.model_outcome for r in caplog.records] == ["provider_error", "success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_summary_recovery_does_not_repeat_executed_query(
    repository, monkeypatch, unavailable
):
    agent, _, executor, orchestrator = await harness(repository, [])
    director, _ = vertex([])
    director.model = "gemini-3.7-flash"
    agent.director = director
    responses = [
        selection(),
        query(
            metric_terms=["active_users"],
            dimension_terms=["date"],
            time_grain="day",
            period={"start": "2026-05-01", "end": "2026-05-31"},
        ),
        selection("evidence.answer"),
    ]
    summary_calls = []

    async def generate(**kwargs):
        if responses:
            return SimpleNamespace(text=json.dumps(responses.pop(0)))
        summary_calls.append(kwargs["contents"])
        if unavailable or len(summary_calls) == 1:
            raise ClientError(
                429 if unavailable else 503, {"error": {"message": "temporary"}}
            )
        facts = json.loads(kwargs["contents"])["facts"]
        return SimpleNamespace(
            text=json.dumps(
                {
                    "explanation": "取得済みの推移を確認しました。",
                    "assessments": [
                        {
                            "interpretation": "日ごとの違いを確認できます。",
                            "fact_ids": [facts[0]["id"]],
                        }
                    ],
                }
            )
        )

    async def no_wait(_):
        return None

    monkeypatch.setattr("analytics_ai.agent.director.asyncio.sleep", no_wait)
    director.client.aio.models.generate_content = generate
    await run_turn(agent, "2026年5月のUUを日別に。比較不要です。", "retry-summary")
    workspace = await agent.workspace("user-a", "as_orchestrator")
    assert workspace.status == ("partial" if unavailable else "complete")
    if unavailable:
        assert "agent_model_rate_limited" in workspace.execution_errors
    else:
        assert not workspace.execution_errors
    assert len(executor.invocations) == 1
    assert len(summary_calls) == 2 and summary_calls[0] == summary_calls[1]
    session = await agent.session("user-a", "as_orchestrator")
    messages = await repository.list_messages(session.id)
    charts = await orchestrator.restore_results(session=session, messages=messages)
    assert len(charts) == 1
    if unavailable:
        assert "HTTP 429" in messages[-1].content
        assert "GA4 APIの取得上限とは別" in messages[-1].content
    else:
        assert "処理状況" not in messages[-1].content
