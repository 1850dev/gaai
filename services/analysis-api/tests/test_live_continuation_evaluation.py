"""Offline checks for the opt-in live-model evaluation, not model quality tests."""

from types import SimpleNamespace

import pytest
from evaluation.live_continuation import (
    APRIL,
    Budget,
    build,
    evaluate,
    mismatch,
    turn,
)


@pytest.mark.asyncio
async def test_default_self_test_does_not_construct_live_director(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("live_model_not_opted_in")

    monkeypatch.setattr("evaluation.live_continuation.VertexAgentDirector", forbidden)
    await evaluate(1, False)
    assert '"seed": "complete"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_budget_caps_actual_provider_attempts():
    attempts = []

    async def original(**kwargs):
        attempts.append(kwargs)
        return SimpleNamespace(usage_metadata=SimpleNamespace(total_token_count=5))

    director = SimpleNamespace(
        client=SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=original)),
        )
    )
    budget = Budget(maximum=2)
    budget.bind(director)
    for _ in range(2):
        await director.client.aio.models.generate_content()
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        await director.client.aio.models.generate_content()
    assert len(attempts) == 2 and budget.tokens == 10


@pytest.mark.asyncio
async def test_safety_controls_ask_and_reuse_without_model_or_analytics():
    agent, _, executor = await build()
    await turn(agent, "月別に参照元をセッション数とUUで比較", "seed")

    async def forbidden(*args, **kwargs):
        raise AssertionError("safety_path_must_not_plan")

    agent.director.decide = forbidden
    ambiguous = await turn(agent, "日別か月別のどちらかに変更して", "ambiguous")
    assert ambiguous[-1]["data"]["status"] == "waiting"
    assert ambiguous[-1]["data"]["questions"]
    no_fetch = await turn(
        agent,
        "新しいデータ取得はせず、直前の取得結果と条件を説明してください。レポートは作成しないでください。",
        "no-fetch",
    )
    assert no_fetch[-1]["data"]["status"] == "complete"
    assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("metrics", ("active_users",)),
        ("dimensions", ("year_month", "landing_page")),
        ("comparison_period", APRIL),
    ],
)
async def test_oracle_detects_unrequested_model_changes(field, value):
    agent, repository, _ = await build()
    await turn(agent, "月別に参照元をセッション数とUUで比較", "seed")
    state = (await repository.get_session("as_synthetic_eval")).state
    assert mismatch(state.model_copy(update={field: value}), state) == [field]
