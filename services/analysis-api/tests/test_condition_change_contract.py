"""Q3b: change exactly the requested condition, retain the other conditions."""

from __future__ import annotations

import pytest

from analytics_ai.agent.continuation_contract import condition_change_continuation
from analytics_ai.agent.director import ga4_argument_context
from analytics_ai.agent.models import AgentWorkspace
from analytics_ai.agent.service import AgentService
from analytics_ai.cache.key import cache_key
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import PreparedInvocation
from analytics_ai.orchestrator.planning import IntentPlan, PlanCompiler, TimeGrain
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import finish
from tests.test_agent_continuation import (
    ContinuationExecutor,
    comparison_plan,
    configure_continuation,
    query,
    run_turn,
)
from tests.test_explicit_period_comparison import APRIL, JUNE, MAY
from tests.test_narrowing_contract import anchored
from tests.test_planning import authorized_scope
from tests.test_time_grain_intent import register_time


class TimeExecutor(ContinuationExecutor):
    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult:
        self.invocations.append(invocation)
        rows = []
        for index, period in enumerate(invocation.arguments["date_ranges"]):
            day = period["start_date"].replace("-", "")
            values = {
                "sessionSource": "google",
                "landingPage": "/articles",
                "date": day,
                "yearMonth": day[:6],
            }
            rows.append(
                {
                    **{f: values[f] for f in invocation.arguments["dimensions"]},
                    **{f: 120 - index * 20 for f in invocation.arguments["metrics"]},
                    "dateRange": period["name"],
                }
            )
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["baseline", "axis", "grain"])
async def test_explicit_changes_preserve_unrequested_conditions(
    repository: InMemoryRepository,
    case: str,
) -> None:
    initial = comparison_plan().model_copy(update={"device_term": "mobile"})
    if case == "baseline":
        question = "比較基準だけ2026年4月に変更してください"
        plans = [
            IntentPlan(
                intent="誤ってUUだけ",
                comparison_period=APRIL,
                metric_terms=("active_users",),
            ),
            IntentPlan(intent="比較元だけ変更", comparison_period=APRIL),
        ]
    elif case == "axis":
        question = "参照元からランディングページに変更してください"
        plans = [IntentPlan(intent="軸だけ変更", dimension_terms=("landing_page",))]
    else:
        initial = initial.model_copy(update={"time_grain": TimeGrain.MONTH})
        question = "月別から日別に変更してください"
        plans = [IntentPlan(intent="粒度だけ変更", time_grain=TimeGrain.DAY)]
    agent, director, _ = await configure_continuation(
        repository,
        [query(initial), finish(), *(query(p) for p in plans), finish()],
    )
    await register_time(repository)
    executor = TimeExecutor()
    assert agent.orchestrator is not None
    agent.orchestrator._executor = executor
    first = await run_turn(agent, "参照元の比較をモバイルで確認", "initial")
    assert first[-1]["data"]["status"] == "complete"
    result = await run_turn(agent, question, "changed")
    assert result[-1]["data"]["status"] == "complete"
    assert len(executor.invocations) == 2
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert state.metrics == ("sessions", "active_users")
    assert state.period == JUNE
    assert state.comparison_period == (APRIL if case == "baseline" else MAY)
    assert [(f.field_name, f.value) for f in state.filters] == [
        ("deviceCategory", "mobile")
    ]
    expected_axes = {"axis": ("landing_page",), "grain": ("date", "session_source")}
    assert state.dimensions == expected_axes.get(case, ("session_source",))
    workspace = await agent.workspace("user-a", "as_orchestrator")
    assert not workspace.report_requested
    if case == "baseline":
        assert any(
            "followup_context_changed" in (c.get("validation_error") or "")
            for c in director.contexts
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,field,value",
    [
        ("比較基準だけ2026年4月に変更してください", "comparison_period", APRIL),
        ("比較期間を2026年4月へ変えて", "comparison_period", APRIL),
        ("同じ条件で比較元のみ2026年4月にして", "comparison_period", APRIL),
        ("比較だけ解除してください", "comparison_period", None),
        ("期間比較をなしにして", "comparison_period", None),
        (
            "参照元からランディングページに変更してください",
            "dimensions",
            ("landing_page",),
        ),
        ("流入元をランディングページへ切り替えて", "dimensions", ("landing_page",)),
        ("月別から日別に変更してください", "time_grain", "day"),
        ("月次を日付にして", "time_grain", "day"),
    ],
)
async def test_contract_declares_changed_and_retained_fields(
    repository,
    question,
    field,
    value,
):
    state = await anchored(repository)
    await register_time(repository)
    if field == "time_grain":
        state = state.model_copy(
            update={
                "dimensions": ("year_month", "session_source"),
                "time_grain": "month",
            }
        )
    contract = condition_change_continuation(
        question,
        state,
        list(repository.semantic_definitions.values()),
    )
    assert contract is not None
    assert getattr(contract.expected, field) == value
    assert field in contract.changed_fields
    assert contract.expected.period == state.period
    assert contract.expected.metrics == state.metrics
    assert contract.expected.filters == state.filters
    assert contract.anchor == state
    context = contract.context()
    assert context["id"] == "analysis.condition_change.v1"
    assert (
        ga4_argument_context({"continuation_contract": context})[
            "continuation_contract"
        ]
        == context
    )
    requirements = contract.requirements(list(repository.semantic_definitions.values()))
    assert requirements["comparison_required_exactly"] is True
    if field == "dimensions":
        assert requirements["required_dimension_fields"] == ("landingPage",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "比較基準を2026年13月に変更して",
        "比較基準を2026年4月15日に変更して",
        "比較基準を2026年4月か5月に変更して",
        "比較基準を2026年4月に変更しないで",
        "比較基準だけ2026年4月に変更して。指標はUUだけ",
        "比較だけ解除して。Organicも外して",
        "新規取得はせず参照元からランディングページに変更して",
        "参照元から未知の項目に変更して",
        "参照元からランディングページに変更してもいいですか",
        "参照元からランディングページに変更しないで",
        "参照元とランディングページを掛け合わせて",
        "参照元からランディングページに変更してレポートを作って",
        "参照元から日付に変更して",
        "月別から日別に変更して",  # No monthly axis in this anchor.
    ],
)
async def test_other_tasks_are_not_misclassified_as_a_single_edit(repository, question):
    state = await anchored(repository)
    await register_time(repository)
    assert (
        condition_change_continuation(
            question,
            state,
            list(repository.semantic_definitions.values()),
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "比較基準だけ2026年4月に変更して",
        "比較だけ解除して",
        "参照元からランディングページに変更して",
    ],
)
async def test_omitted_edit_cannot_validate_against_the_expected_state(
    repository, question
):
    state = await anchored(repository)
    definitions = list(repository.semantic_definitions.values())
    contract = condition_change_continuation(question, state, definitions)
    assert contract is not None
    compiled = PlanCompiler().compile(
        IntentPlan(intent="変更を忘れた計画"),
        state=state,
        scope=await authorized_scope(repository),
        definitions=definitions,
    )
    with pytest.raises(ValueError, match="followup_context_changed"):
        contract.validate(compiled)


@pytest.mark.asyncio
async def test_unavailable_or_ambiguous_target_alias_does_not_select_an_axis(
    repository,
):
    state = await anchored(repository)
    definitions = list(repository.semantic_definitions.values())
    question = "参照元からランディングページに変更して"
    unavailable = [
        d.model_copy(update={"available": False})
        if d.canonical_name == "landing_page"
        else d
        for d in definitions
    ]
    assert condition_change_continuation(question, state, unavailable) is None
    landing = next(d for d in definitions if d.canonical_name == "landing_page")
    duplicate = landing.model_copy(update={"canonical_name": "other_landing"})
    assert (
        condition_change_continuation(question, state, [*definitions, duplicate])
        is None
    )
    assert (
        condition_change_continuation(
            question, state.model_copy(update={"period": None}), definitions
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_comparison_is_distinct_from_unconstrained_completion(repository):
    agent, _, _ = await configure_continuation(
        repository, [query(comparison_plan()), finish()]
    )
    await run_turn(agent, "参照元の比較", "initial")
    workspace = await agent.workspace("user-a", "as_orchestrator")
    state = (await agent.session("user-a", "as_orchestrator")).state
    contract = condition_change_continuation(
        "比較だけ解除して", state, list(repository.semantic_definitions.values())
    )
    assert contract is not None
    required = workspace.model_copy(
        update=contract.requirements(list(repository.semantic_definitions.values()))
    )
    assert AgentService.missing_fields(required)
    # Existing records missing the new flag remain readable and unconstrained.
    legacy = AgentWorkspace.model_validate(
        required.model_dump(exclude={"comparison_required_exactly"})
    )
    assert not legacy.comparison_required_exactly
    assert not AgentService.missing_fields(legacy)


@pytest.mark.asyncio
async def test_edits_and_narrowing_compose_across_six_turns(repository):
    agent, director, _ = await configure_continuation(
        repository,
        [
            query(comparison_plan().model_copy(update={"time_grain": TimeGrain.MONTH})),
            finish(),
            query(IntentPlan(intent="Organic", channel_term="organic_search")),
            finish(),
            query(IntentPlan(intent="比較元", comparison_period=APRIL)),
            finish(),
            query(IntentPlan(intent="日別", time_grain=TimeGrain.DAY)),
            finish(),
            query(IntentPlan(intent="LP", dimension_terms=("date", "landing_page"))),
            finish(),
            query(IntentPlan(intent="比較解除", clear_comparison=True)),
            finish(),
        ],
    )
    await register_time(repository)
    executor = TimeExecutor()
    agent.orchestrator._executor = executor
    for index, question in enumerate(
        [
            "月別に参照元を比較",
            "Organicだけ",
            "比較基準だけ2026年4月に変更して",
            "月別から日別に変更して",
            "参照元からランディングページに変更して",
            "比較だけ解除して",
        ]
    ):
        result = await run_turn(agent, question, f"chain-{index}")
        assert result[-1]["data"]["status"] == "complete", (index, result[-1])
    assert len(executor.invocations) == 6
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert state.period == JUNE and state.comparison_period is None
    assert state.dimensions == ("date", "landing_page") and state.time_grain == "day"
    assert state.metrics == ("sessions", "active_users")
    assert [(f.field_name, f.value) for f in state.filters] == [
        ("sessionDefaultChannelGroup", "Organic Search")
    ]
    assert len(executor.invocations[-1].arguments["date_ranges"]) == 1
    workspace = await agent.workspace("user-a", "as_orchestrator")
    assert not workspace.report_requested
    assert workspace.comparison_required_exactly
    assert not AgentService.missing_fields(workspace)
    assert any(
        c.get("continuation_contract", {}).get("id") == "analysis.condition_change.v1"
        for c in director.contexts
        if c.get("continuation_contract")
    )


@pytest.mark.asyncio
async def test_comparison_change_has_its_own_cache_key(repository):
    state = await anchored(repository)
    scope = await authorized_scope(repository)
    definitions = list(repository.semantic_definitions.values())
    compiler = PlanCompiler()
    plans = [
        IntentPlan(intent="維持"),
        IntentPlan(intent="変更", comparison_period=APRIL),
        IntentPlan(intent="解除", clear_comparison=True),
    ]
    compiled = [
        compiler.compile(p, state=state, scope=scope, definitions=definitions)
        for p in plans
    ]
    assert len({cache_key(c.descriptor) for c in compiled}) == 3


@pytest.mark.asyncio
async def test_subsequent_plan_is_validated_against_the_actual_compilation_state(
    repository,
):
    state = await anchored(repository)
    definitions = list(repository.semantic_definitions.values())
    contract = condition_change_continuation(
        "比較基準だけ2026年4月に変更して", state, definitions
    )
    assert contract is not None
    # A later plan may inherit the already-committed, correctly changed baseline.
    compiled = PlanCompiler().compile(
        IntentPlan(intent="同じ条件"),
        state=contract.expected,
        scope=await authorized_scope(repository),
        definitions=definitions,
    )
    contract.validate(compiled, state=contract.expected)
