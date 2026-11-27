from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from analytics_ai.agent.director import (
    VertexAgentDirector,
    generation_schema,
    validation_feedback,
)
from analytics_ai.agent.models import AgentDecision, AgentSummary, GroundedAssessment
from analytics_ai.agent.presentation import render_summary
from analytics_ai.agent.research import explicit_half_year_review
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.domain.models import AnalysisState, SemanticDefinition, SemanticScope
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.orchestrator.planning import ComparisonIntent, IntentPlan
from analytics_ai.orchestrator.service import AnalysisOrchestrationError
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director
from tests.test_orchestrator import add_session, build_orchestrator

QUESTION = (
    "2026年上期のpage viewとアクティブユーザーについて"
    "多角的に分析して上期の評価をしてください"
)


class ReviewExecutor:
    def __init__(self, fail_pages: bool = False) -> None:
        self.invocations: list[Any] = []
        self.fail_pages = fail_pages

    async def execute(self, invocation: Any) -> ToolExecutionResult:
        self.invocations.append(invocation)
        dimensions = invocation.arguments["dimensions"]
        if "pagePath" in dimensions and self.fail_pages:
            raise AnalysisOrchestrationError("mcp_result_row_limit_exceeded")
        rows = []
        categories = [None]
        if dimensions:
            categories = (
                ["202601", "202602", "202603", "202604", "202605", "202606"]
                if "yearMonth" in dimensions
                else ["Organic Search", "Direct"]
                if "sessionDefaultChannelGroup" in dimensions
                else ["/", "/news"]
            )
        for comparison in [False, True]:
            for index, category in enumerate(categories):
                row: dict[str, Any] = {
                    "screenPageViews": 900 + index * 20
                    if comparison
                    else 1000 + index * 30,
                    "activeUsers": 90 + index * 2 if comparison else 100 + index * 3,
                    "dateRange": "comparison" if comparison else "current",
                }
                if dimensions:
                    row[dimensions[0]] = category
                rows.append(row)
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )


def initial_plan() -> IntentPlan:
    return IntentPlan(
        intent="上期のPVとアクティブユーザーを評価",
        period={"start": "2026-01-01", "end": "2026-06-30"},
        metric_terms=("page_views", "active_users"),
        comparison=ComparisonIntent.PREVIOUS_YEAR,
    )


async def configure_review(
    repository: InMemoryRepository, *, fail_pages: bool = False
) -> tuple[AgentService, Director, ReviewExecutor]:
    await add_session(repository)
    original = repository.semantic_definitions["global-active-users"]
    repository.semantic_definitions[original.id] = original.model_copy(
        update={"aliases": (*original.aliases, "アクティブユーザー")}
    )
    for name, kind, mapping in (
        ("page_views", "metric", {"metric": "screenPageViews"}),
        (
            "session_channel_group",
            "dimension",
            {"dimension": "sessionDefaultChannelGroup"},
        ),
        ("page_path", "dimension", {"dimension": "pagePath"}),
    ):
        item = SemanticDefinition(
            id=f"test-{name}",
            canonical_name=name,
            aliases=(name,),
            kind=kind,
            scope=SemanticScope.GLOBAL,
            version="a-web-v1",
            ga4_mapping=mapping,
            aggregation="sum" if kind == "metric" else None,
        )
        repository.semantic_definitions[item.id] = item
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="上期評価",
                explanation="GA4の2026年上期を確認します。",
                plan=initial_plan(),
            )
        ]
    )
    orchestrator, _ = build_orchestrator(repository, [], {})
    executor = ReviewExecutor(fail_pages)
    orchestrator._executor = executor
    return (
        AgentService(repository, MemoryAgentStore(), director, orchestrator),
        director,
        executor,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_pages", [False, True])
async def test_half_year_review_finishes_with_coverage_and_evidence(
    repository: InMemoryRepository, fail_pages: bool
) -> None:
    agent, director, executor = await configure_review(
        repository, fail_pages=fail_pages
    )
    events = [
        event
        async for event in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content=QUESTION,
            operation_id="review",
        )
    ]
    assert events[-1]["event"] == "done", events[-1]
    workspace = events[-1]["data"]
    assert workspace["status"] == ("partial" if fail_pages else "complete")
    assert workspace["review_targets"] == [
        "期間全体",
        "時系列",
        "流入チャネル",
        "主要ページ",
    ]
    assert len(workspace["completed_perspectives"]) == (3 if fail_pages else 4)
    assert len(executor.invocations) == 4
    assert (
        len(director.contexts) == 1
    )  # explicit reviewed intent + independent assessment
    assert not workspace["report_requested"]
    for call in executor.invocations:
        assert call.arguments["property_id"] == "222222222"
        assert call.arguments["date_ranges"][0]["start_date"] == "2026-01-01"
        assert call.arguments["date_ranges"][0]["end_date"] == "2026-06-30"
    assert executor.invocations[1].arguments["dimensions"] == ["yearMonth"]
    context = director.contexts[-1]
    assert len(context["facts"]) == (6 if fail_pages else 8)
    assert all(f["period"]["start"] == "2026-01-01" for f in context["facts"])
    assert all("calculations" in f for f in context["facts"])
    answer = (await repository.list_messages("as_orchestrator"))[-1]
    assert answer.finding_ids and answer.evidence_ids
    assert "根拠:" in answer.content
    if fail_pages:
        assert "主要ページ" in answer.content
        assert "未確認の観点" in answer.content
        assert "取得結果がアプリの返却件数上限" in answer.content


def test_summary_contract_has_no_execution_fields_and_rejects_fabricated_numbers() -> (
    None
):
    schema = generation_schema(AgentSummary)
    assert "IntentPlan" not in schema.get("$defs", {})
    assert not {"plan", "observed_filters", "proposals"}.intersection(
        schema["properties"]
    )
    with pytest.raises(ValidationError) as rejected:
        AgentSummary(explanation="2026年にPVが12%改善しました")
    feedback = validation_feedback(rejected.value)
    assert "explanation" in feedback
    assert "2026年" not in feedback
    assert AgentSummary(explanation="GA4の取得結果を評価します。")


@pytest.mark.asyncio
async def test_summary_repairs_numeric_prose_once_and_preserves_fact_grounding() -> (
    None
):
    calls: list[dict[str, Any]] = []
    responses = [
        {"explanation": "2026年の評価です"},
        {
            "explanation": "取得結果の評価です。",
            "assessments": [
                {
                    "interpretation": "利用規模は増加しています。",
                    "fact_ids": ["finding_one"],
                }
            ],
        },
        {
            "source_reasoning": "No inferred source.",
            "measurement_reasoning": "No measurement claim.",
            "capability_reasoning": "No unsupported tool.",
            "purpose_reasoning": "No invented goal.",
            "purpose_coverage": [],
            "issues": [],
        },
    ]

    async def generate(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(responses.pop(0), ensure_ascii=False))

    director = object.__new__(VertexAgentDirector)
    director.model = "synthetic-model"
    director.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )
    summary = await director.summarize({"facts": [{"id": "finding_one"}]})
    assert summary.assessments[0].fact_ids == ("finding_one",)
    assert len(calls) == 3
    assert "explanation" in json.loads(calls[1]["contents"])["validation_error"]


def test_unavailable_summary_still_evaluates_only_period_kpis() -> None:
    facts = [
        {
            "id": "one",
            "metric": "activeUsers",
            "label": "アクティブユーザー数",
            "text": "UU: 100",
            "formatted_text": "UU: 100",
            "dimension_field": None,
            "change_rate": "0.1",
            "evidence_ids": ["evidence_one"],
        }
    ]
    body = render_summary(None, facts, ("期間全体", "主要ページ"), ("期間全体",))
    assert "増加しています" in body
    assert "未確認の観点: 主要ページ" in body
    assert "evidence_one" in body
    with pytest.raises(ValueError, match="unknown_fact"):
        render_summary(
            AgentSummary(
                explanation="評価",
                assessments=(
                    GroundedAssessment(interpretation="増加", fact_ids=("invented",)),
                ),
            ),
            facts,
            (),
            (),
        )


@pytest.mark.asyncio
async def test_review_keeps_filters_and_graphs_reusable(
    repository: InMemoryRepository,
) -> None:
    agent, _, executor = await configure_review(repository)
    session = await agent.session("user-a", "as_orchestrator")
    # The scope selected by the user remains registered/authorized at every step.
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content=QUESTION,
            operation_id="graphs",
        )
    ]
    assert events[-1]["event"] == "done"
    assert agent.orchestrator is not None
    visualization = await agent.orchestrator.analyze(
        user_id="user-a",
        session_id=session.id,
        content="今のグラフ",
        request_id="chart",
        trusted_plan=IntentPlan(intent="visualize", visualization_only=True),
    )
    assert visualization.rows
    assert len(executor.invocations) == 4


@pytest.mark.asyncio
async def test_half_year_seed_defers_fiscal_and_filter_requests(
    repository: InMemoryRepository,
) -> None:
    await configure_review(repository)
    definitions = list(repository.semantic_definitions.values())
    state = AnalysisState(
        data_source_set_id="company_a_web", semantic_version="a-web-v1"
    )
    plan = explicit_half_year_review(QUESTION, definitions, state)
    assert plan and plan.period and plan.period.start.isoformat() == "2026-01-01"
    assert set(plan.metric_terms) == {"active_users", "page_views"}
    assert (
        explicit_half_year_review(
            QUESTION.replace("2026年", "2026年度"), definitions, state
        )
        is None
    )
    assert (
        explicit_half_year_review(QUESTION + "Organicだけ", definitions, state) is None
    )


@pytest.mark.asyncio
async def test_revocation_during_summary_prevents_answer_release(
    repository: InMemoryRepository,
) -> None:
    agent, director, _ = await configure_review(repository)
    original_summary = director.summarize

    async def revoked(context: dict[str, Any]) -> AgentSummary:
        answer = await original_summary(context)
        repository.grants.clear()
        return answer

    director.summarize = revoked
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content=QUESTION,
            operation_id="revoked",
        )
    ]
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["code"] == "access_denied"
