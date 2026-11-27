"""Q3a: short filter followups must not silently change the analysis task."""

from __future__ import annotations

import pytest

from analytics_ai.agent.capabilities import effective_definitions
from analytics_ai.agent.continuation_contract import filter_continuation
from analytics_ai.agent.director import ga4_argument_context
from analytics_ai.domain.models import AnalysisState
from analytics_ai.orchestrator.planning import IntentPlan, PlanCompiler
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import finish
from tests.test_agent_continuation import (
    comparison_plan,
    configure_continuation,
    query,
    run_turn,
)
from tests.test_explicit_period_comparison import APRIL, JUNE, MAY
from tests.test_planning import authorized_scope


async def anchored(repository: InMemoryRepository) -> AnalysisState:
    agent, _, _ = await configure_continuation(
        repository, [query(comparison_plan()), finish()]
    )
    await run_turn(agent, "参照元別セッション数とUUを比較して", "initial")
    return (await agent.session("user-a", "as_orchestrator")).state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "その中でモバイルだけ",
        "モバイルだけに絞ってください",
        "同じ条件でOrganic Searchのみ",
        "Organicとモバイルだけ",
        "その条件で mobile のみに限定してください。",
    ],
)
async def test_short_positive_commands_have_an_explicit_contract(
    repository: InMemoryRepository,
    question: str,
) -> None:
    state = await anchored(repository)
    contract = filter_continuation(
        question, state, list(repository.semantic_definitions.values())
    )
    assert contract is not None
    assert contract.expected.period == JUNE
    assert contract.expected.comparison_period == MAY
    assert contract.expected.metrics == state.metrics
    assert contract.expected.dimensions == state.dimensions
    assert contract.expected.filters
    context = contract.context()
    assert (
        ga4_argument_context({"continuation_contract": context})[
            "continuation_contract"
        ]
        == context
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "2026年5月はモバイルだけ",
        "その中でモバイル以外",
        "モバイルだけにはしないで",
        "モバイルだけの改善案を提案して",
        "モバイルまたはOrganicだけ",
        "モバイルだけ。比較は不要",
        "モバイルだけ、指標はPVに変更",
        "モバイルだけを国別に分析して",
        "新しいデータ取得はせずモバイルだけを説明",
        "モバイルだけ。レポートも作成して",
        "その中で未知の端末だけ",
        "前月と比較して",
    ],
)
async def test_new_or_ambiguous_tasks_are_not_frozen_as_narrowing(
    repository: InMemoryRepository,
    question: str,
) -> None:
    state = await anchored(repository)
    assert (
        filter_continuation(
            question, state, list(repository.semantic_definitions.values())
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        IntentPlan(intent="期間変更", period=APRIL, device_term="mobile"),
        IntentPlan(intent="比較解除", clear_comparison=True, device_term="mobile"),
        IntentPlan(intent="比較元変更", comparison_period=APRIL, device_term="mobile"),
        IntentPlan(intent="条件不足"),
    ],
)
async def test_compiled_period_and_filter_changes_are_rejected(
    repository: InMemoryRepository,
    plan: IntentPlan,
) -> None:
    state = await anchored(repository)
    definitions = list(repository.semantic_definitions.values())
    contract = filter_continuation("モバイルだけ", state, definitions)
    assert contract is not None
    compiled = PlanCompiler().compile(
        plan,
        state=state,
        scope=await authorized_scope(repository),
        definitions=definitions,
    )
    with pytest.raises(ValueError, match="followup_context_changed"):
        contract.validate(compiled)


@pytest.mark.asyncio
async def test_missing_or_unavailable_context_does_not_guess_new_terms(
    repository: InMemoryRepository,
) -> None:
    state = await anchored(repository)
    definitions = list(repository.semantic_definitions.values())
    assert (
        filter_continuation(
            "モバイルだけ", state.model_copy(update={"period": None}), definitions
        )
        is None
    )
    session = repository.sessions["as_orchestrator"]
    definitions = [
        d.model_copy(update={"available": False, "ga4_mapping": None})
        if d.canonical_name == "mobile"
        else d
        for d in definitions
    ]
    assert (
        filter_continuation(
            "モバイルだけ", state, effective_definitions(definitions, session)
        )
        is None
    )


@pytest.mark.asyncio
async def test_organic_then_mobile_preserves_both_filters_and_original_axes(
    repository: InMemoryRepository,
) -> None:
    agent, _, executor = await configure_continuation(
        repository,
        [
            query(comparison_plan()),
            finish(),
            query(IntentPlan(intent="Organic", channel_term="organic_search")),
            finish(),
            query(IntentPlan(intent="mobile", device_term="mobile")),
            finish(),
        ],
    )
    for index, question in enumerate(
        (
            "参照元別セッション数とUUを比較して",
            "Organicだけ",
            "その中でモバイルだけ",
        )
    ):
        result = await run_turn(agent, question, f"filter-chain-{index}")
        assert result[-1]["data"]["status"] == "complete"
    assert len(executor.invocations) == 3
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert {(f.field_name, f.value) for f in state.filters} == {
        ("sessionDefaultChannelGroup", "Organic Search"),
        ("deviceCategory", "mobile"),
    }
    assert state.metrics == ("sessions", "active_users")
    assert state.dimensions == ("session_source",)
    assert state.period == JUNE and state.comparison_period == MAY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong",
    [
        IntentPlan(
            intent="誤った指標", metric_terms=("active_users",), device_term="mobile"
        ),
        IntentPlan(
            intent="誤った軸", dimension_terms=("landing_page",), device_term="mobile"
        ),
    ],
)
async def test_narrowing_replans_before_executing_changed_conditions(
    repository: InMemoryRepository, wrong: IntentPlan
) -> None:
    agent, director, executor = await configure_continuation(
        repository,
        [
            query(comparison_plan()),
            finish(),
            query(wrong),
            query(IntentPlan(intent="元の条件のモバイル", device_term="mobile")),
            finish(),
        ],
    )
    await run_turn(agent, "参照元別セッション数とUUを比較して", "initial")
    result = await run_turn(agent, "その中でモバイルだけ", "narrow")
    assert result[-1]["data"]["status"] == "complete"
    # The incorrect plan must not consume a GA4 call or replace the anchor.
    assert len(executor.invocations) == 2
    invocation = executor.invocations[-1]
    assert invocation.arguments["metrics"] == ["sessions", "activeUsers"]
    assert invocation.arguments["dimensions"] == ["sessionSource"]
    assert (
        invocation.arguments["date_ranges"]
        == executor.invocations[0].arguments["date_ranges"]
    )
    assert any(
        "followup_context_changed" in (c.get("validation_error") or "")
        for c in director.contexts
    )
    workspace = await agent.workspace("user-a", "as_orchestrator")
    assert workspace.required_dimension_fields == ("sessionSource",)
    assert workspace.required_metric_fields == ("sessions", "activeUsers")
    assert not workspace.report_requested


@pytest.mark.asyncio
async def test_narrowing_cannot_finish_with_wrong_axis_and_lose_comparison(
    repository: InMemoryRepository,
) -> None:
    original = comparison_plan()
    wrong = original.model_copy(
        update={
            "dimension_terms": ("landing_page",),
            "metric_terms": ("active_users",),
            "comparison_period": None,
            "clear_comparison": True,
            "device_term": "mobile",
        }
    )
    agent, _, executor = await configure_continuation(
        repository, [query(original), finish(), query(wrong), finish(), finish()]
    )
    await run_turn(agent, "参照元別セッション数とUUを比較して", "initial")
    before = (await agent.session("user-a", "as_orchestrator")).state
    result = await run_turn(agent, "モバイルだけに絞ってください", "narrow")
    assert result[-1]["data"]["status"] == "partial"
    assert len(executor.invocations) == 1
    after = (await agent.session("user-a", "as_orchestrator")).state
    assert after == before
