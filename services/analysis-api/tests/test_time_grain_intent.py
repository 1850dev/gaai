"""Q2b-1: one grain interpretation for queries, state and completion."""

from datetime import date

import pytest

from analytics_ai.agent.models import AgentDecision
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.agent.workflows import explicit_dimension_fields
from analytics_ai.config import Settings
from analytics_ai.domain.models import AnalysisState, DatePeriod
from analytics_ai.ga4.chat_sessions import Ga4ChatSessionService
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.orchestrator.planning import (
    IntentPlan,
    TimeGrain,
    VertexIntentPlanner,
)
from analytics_ai.orchestrator.time_grain import time_grain_request
from tests.test_agent import Director, finish
from tests.test_agent_continuation import run_turn
from tests.test_orchestrator import add_session, build_orchestrator
from tests.test_planning import DailyGrainGenerator

MONTH_REQUEST = (
    "同じ期間のアクティブユーザー数を月別にしてください。"
    "日別の合計ではなく月単位のユーザー数を取得してください。"
)


@pytest.mark.parametrize(
    "question,expected",
    [
        (MONTH_REQUEST, "month"),
        ("月別ではなく日別のUUを取得", "day"),
        ("日別ではなく、月別のUU", "month"),
        ("週次ではなく年次で", "year"),
        ("年別ではなく週別で", "week"),
        ("日別から月別に変更してください", "month"),
        ("月別を日別に切り替えて", "day"),
        ("日別は不要。月別で", "month"),
        ("日別で集計しない。月別で", "month"),
        ("日別で表示。期間比較はしない", "day"),
        ("月別で表示してレポートは不要", "month"),
        ("月別のUUを取得。新しいレポートは作らない", "month"),
        ("monthly, not daily", "month"),
        ("not monthly but daily", "day"),
        ("monthly instead of daily", "month"),
        ("from daily to monthly", "month"),
        ("UU by month", "month"),
        ("日次", "day"),
        ("週次", "week"),
        ("年次", "year"),
        ("2026年5月のUU", None),
        ("広告は不要ではない", None),
        ("月別で表示。広告は不要ではない", "month"),
    ],
)
def test_affirmative_grain_not_first_keyword(question, expected):
    result = time_grain_request(question)
    assert result.single == expected
    assert not result.needs_clarification


@pytest.mark.parametrize(
    "question",
    [
        "日別か月別で",
        "日別 か 月別で",
        "日別 または 月別で",
        "daily or monthly",
        "日別と月別どちらかで",
        "日別は不要",
        "日別は不要ではない",
    ],
)
def test_ambiguous_or_only_negative_grain_is_not_guessed(question):
    result = time_grain_request(question)
    assert result.single is None
    assert result.needs_clarification


@pytest.mark.parametrize(
    "question",
    [
        "日別と月別の両方",
        "月別・日別で",
        "daily and monthly",
        "日別と月別で。表示は表かグラフのどちらか",
    ],
)
def test_both_grains_are_not_reduced_to_first(question):
    result = time_grain_request(question)
    assert set(result.requested) == {TimeGrain.DAY, TimeGrain.MONTH}
    assert result.single is None
    assert not result.needs_clarification


async def register_time(repository):
    source = await repository.get_data_source_set("company_a_web")
    definitions = list(Ga4ChatSessionService._semantic_definitions(source))
    for definition in definitions:
        if definition.kind == "date":
            repository.semantic_definitions[definition.id] = definition
    return definitions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,expected",
    [
        (MONTH_REQUEST, {"yearMonth"}),
        ("月別ではなく日別", {"date"}),
        ("日別と月別の両方", {"date", "yearMonth"}),
        ("週別ではなく年別", {"year"}),
        ("日別か月別", set()),
    ],
)
async def test_coverage_excludes_negated_semantic_date_alias(
    repository, question, expected
):
    definitions = await register_time(repository)
    assert set(explicit_dimension_fields(question, definitions)) == expected


@pytest.mark.asyncio
async def test_single_planner_overrides_stale_model_grain():
    generator = DailyGrainGenerator()
    planner = VertexIntentPlanner(Settings(environment="test"), generator=generator)
    result = await planner.plan(
        question=MONTH_REQUEST,
        state=AnalysisState(
            data_source_set_id="company_a_web", semantic_version="a-web-v1"
        ),
        definitions=[],
        today=date(2026, 9, 17),
    )
    assert result.time_grain == TimeGrain.MONTH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,error",
    [
        ("日別か月別のUU", "time_grain_clarification_required"),
        ("日別と月別の両方でUU", "multiple_time_grains_require_agent"),
    ],
)
async def test_legacy_single_query_planner_does_not_silently_choose(question, error):
    generator = DailyGrainGenerator()
    planner = VertexIntentPlanner(Settings(environment="test"), generator=generator)
    with pytest.raises(ValueError, match=error):
        await planner.plan(
            question=question,
            state=AnalysisState(
                data_source_set_id="company_a_web", semantic_version="a-web-v1"
            ),
            definitions=[],
            today=date(2026, 9, 17),
        )
    assert not generator.prompt


class TimeExecutor:
    def __init__(self):
        self.invocations = []

    async def execute(self, invocation):
        self.invocations.append(invocation)
        dimensions = invocation.arguments["dimensions"]
        assert dimensions in (["date"], ["yearMonth"])
        rows = (
            [
                {"date": "20260501", "activeUsers": "100"},
                {"date": "20260502", "activeUsers": "100"},
            ]
            if dimensions == ["date"]
            else [{"yearMonth": "202605", "activeUsers": "150"}]
        )
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )


def query(grain: TimeGrain, *, initial=False):
    return AgentDecision(
        action="query",
        objective="UU推移の確認",
        explanation="推移を確認します。",
        plan=IntentPlan(
            intent="UUの推移",
            metric_terms=("active_users",),
            period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))
            if initial
            else None,
            time_grain=grain,
            dimension_terms=("date" if grain == TimeGrain.DAY else "year_month",),
            channel_term="organic_search" if initial else None,
            device_term="mobile" if initial else None,
        ),
    )


async def harness(repository, decisions):
    await add_session(repository)
    await register_time(repository)
    orchestrator, _ = build_orchestrator(repository, [], {})
    executor = TimeExecutor()
    orchestrator._executor = executor
    director = Director(decisions)
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    return agent, director, executor, orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_two_turn_grain_change_preserves_scope_filters_cache_and_charts(
    repository, reverse
):
    initial = TimeGrain.MONTH if reverse else TimeGrain.DAY
    target = TimeGrain.DAY if reverse else TimeGrain.MONTH
    question = (
        "同じ期間で月別ではなく日別のUUを取得してください" if reverse else MONTH_REQUEST
    )
    # Deliberately stale model plans: server-side intent must correct them.
    agent, _, executor, orchestrator = await harness(
        repository,
        [
            query(initial, initial=True),
            finish(),
            query(initial),
            finish(),
            query(initial),
            finish(),
        ],
    )
    first = await run_turn(
        agent, "2026年5月のUUを" + ("月別" if reverse else "日別"), "time-first"
    )
    assert first[-1]["data"]["status"] == "complete"
    second = await run_turn(agent, question, "time-second")
    assert second[-1]["data"]["status"] == "complete", second[-1]
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert state.time_grain == target.value
    assert state.period == DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))
    assert state.focus.channel == "Organic Search"
    assert state.focus.device == "mobile"
    assert len(executor.invocations) == 2
    assert (
        executor.invocations[0].arguments["dimension_filter"]
        == executor.invocations[1].arguments["dimension_filter"]
    )
    assert all(i.arguments["property_id"] == "222222222" for i in executor.invocations)
    workspace = await agent.workspace("user-a", "as_orchestrator")
    assert workspace.required_dimension_fields == (
        ("date",) if reverse else ("yearMonth",)
    )
    messages = await repository.list_messages("as_orchestrator")
    assert "未確認の指定条件" not in messages[-1].content
    charts = await orchestrator.restore_results(
        session=await agent.session("user-a", "as_orchestrator"), messages=messages
    )
    assert len(charts) == 2
    assert len({chart.visualization.title for chart in charts}) == 2
    assert {row["activeUsers"] for chart in charts for row in chart.rows} == {
        "100",
        "150",
    }
    third = await run_turn(agent, question, "time-cache")
    assert third[-1]["data"]["status"] == "complete"
    assert len(executor.invocations) == 2


@pytest.mark.asyncio
async def test_ambiguous_followup_asks_without_ga4_or_model_call(repository):
    agent, director, executor, _ = await harness(repository, [])
    events = await run_turn(agent, "2026年5月のUUを日別か月別で", "time-ask")
    assert events[-1]["data"]["status"] == "waiting", events[-1]
    assert events[-1]["data"]["questions"]
    assert not executor.invocations
    assert not director.contexts


@pytest.mark.asyncio
async def test_both_grains_require_both_results_not_one_daily_result(repository):
    agent, director, executor, _ = await harness(
        repository,
        [
            query(TimeGrain.DAY, initial=True),
            finish(),
            query(TimeGrain.MONTH),
            finish(),
        ],
    )
    events = await run_turn(agent, "2026年5月のUUを日別と月別の両方で", "time-both")
    assert events[-1]["data"]["status"] == "complete", events[-1]
    assert len(executor.invocations) == 2
    assert "yearMonth" in director.contexts[2]["validation_error"]
    assert {tuple(i.arguments["dimensions"]) for i in executor.invocations} == {
        ("date",),
        ("yearMonth",),
    }
