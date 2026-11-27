"""Same-session regressions: inherit scope, reuse evidence, keep authorizing."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from analytics_ai.agent.conversation import (
    existing_result_discussion,
    planning_workspace,
    reusable_sections,
)
from analytics_ai.agent.models import AgentDecision, AgentWorkspace, ReportSection
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import (
    AnalysisFilter,
    AnalysisState,
    DatePeriod,
    SemanticDefinition,
    SemanticScope,
)
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import PreparedInvocation
from analytics_ai.orchestrator.planning import IntentPlan
from analytics_ai.orchestrator.service import AnalysisOrchestrationError
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director, finish
from tests.test_agent_review import QUESTION, configure_review
from tests.test_orchestrator import add_session, build_orchestrator


class ContinuationExecutor:
    """Return only requested fields, so wrong planning cannot pass accidentally."""

    def __init__(self) -> None:
        self.invocations: list[PreparedInvocation] = []

    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult:
        self.invocations.append(invocation)
        dimensions = invocation.arguments["dimensions"]
        metrics = invocation.arguments["metrics"]
        values = {"sessionSource": "google", "landingPage": "/articles"}
        rows = []
        for index, period in enumerate(invocation.arguments["date_ranges"]):
            rows.append(
                {
                    **{field: values[field] for field in dimensions},
                    **{field: 120 - index * 20 for field in metrics},
                    "dateRange": period["name"],
                }
            )
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )


def query(plan: IntentPlan) -> AgentDecision:
    return AgentDecision(
        action="query",
        objective="参照元の分析",
        explanation="必要な内訳を確認します。",
        plan=plan,
    )


def comparison_plan() -> IntentPlan:
    return IntentPlan(
        intent="参照元別のセッションと利用者を比較",
        period=DatePeriod(start=date(2026, 6, 1), end=date(2026, 6, 30)),
        comparison_period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
        metric_terms=("sessions", "active_users"),
        dimension_terms=("session_source",),
    )


async def configure_continuation(
    repository: InMemoryRepository, decisions: list[AgentDecision]
) -> tuple[AgentService, Director, ContinuationExecutor]:
    await add_session(repository)
    for name, kind, field, aliases in (
        ("sessions", "metric", "sessions", ("セッション数",)),
        ("session_source", "dimension", "sessionSource", ("参照元", "流入元")),
        ("landing_page", "dimension", "landingPage", ("ランディングページ",)),
    ):
        item = SemanticDefinition(
            id=f"continuation-{name}",
            canonical_name=name,
            aliases=aliases,
            kind=kind,
            scope=SemanticScope.GLOBAL,
            version="a-web-v1",
            ga4_mapping={kind: field},
            aggregation="sum" if kind == "metric" else None,
        )
        repository.semantic_definitions[item.id] = item
    orchestrator, _ = build_orchestrator(repository, [], {})
    executor = ContinuationExecutor()
    orchestrator._executor = executor
    director = Director(decisions)
    return (
        AgentService(repository, MemoryAgentStore(), director, orchestrator),
        director,
        executor,
    )


async def run_turn(
    agent: AgentService, question: str, operation: str
) -> list[dict[str, Any]]:
    return [
        event
        async for event in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content=question,
            operation_id=operation,
        )
    ]


@pytest.mark.asyncio
async def test_source_to_landing_page_to_mobile_to_evidence_answer(
    repository: InMemoryRepository,
) -> None:
    """Followups intentionally omit dates/metrics instead of restating everything."""
    agent, director, executor = await configure_continuation(
        repository,
        [
            query(comparison_plan()),
            finish(),
            query(
                IntentPlan(
                    intent="参照元ごとのランディングページを深掘り",
                    dimension_terms=("session_source", "landing_page"),
                )
            ),
            finish(),
            query(IntentPlan(intent="モバイルに限定", device_term="mobile")),
            finish(),
            finish(),
        ],
    )
    questions = (
        "2026年5月と2026年6月の参照元別セッション数とUUを比較して",
        "参照元ごとのランディングページを深掘りして",
        "その中でモバイルだけ",
        "今のランディングページの結果を説明してください",
    )
    for index, question in enumerate(questions):
        events = await run_turn(agent, question, f"continuation-{index}")
        assert events[-1]["event"] == "done", events[-1]
        assert events[-1]["data"]["status"] == "complete", events[-1]

    assert len(executor.invocations) == 3
    for invocation in executor.invocations:
        assert invocation.arguments["property_id"] == "222222222"
        assert invocation.arguments["metrics"] == ["sessions", "activeUsers"]
        assert invocation.arguments["date_ranges"] == [
            {"start_date": "2026-06-01", "end_date": "2026-06-30", "name": "current"},
            {
                "start_date": "2026-05-01",
                "end_date": "2026-05-31",
                "name": "comparison",
            },
        ]
    assert executor.invocations[1].arguments["dimensions"] == [
        "sessionSource",
        "landingPage",
    ]
    assert executor.invocations[2].arguments["dimensions"] == [
        "sessionSource",
        "landingPage",
    ]
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert state.focus.device == "mobile"
    assert any(
        f.field_name == "deviceCategory" and f.value == "mobile" for f in state.filters
    )
    answer = (await repository.list_messages("as_orchestrator"))[-1]
    assert answer.finding_ids and answer.evidence_ids
    summary_context = director.contexts[-1]
    assert summary_context["facts"]
    assert all(
        any(
            f["field_name"] == "deviceCategory" and f["value"] == "mobile"
            for f in fact["filters"]
        )
        for fact in summary_context["facts"]
    )


@pytest.mark.asyncio
async def test_new_comparison_cannot_reuse_previous_periods_as_complete(
    repository: InMemoryRepository,
) -> None:
    agent, _, executor = await configure_continuation(
        repository, [query(comparison_plan()), finish(), finish(), finish()]
    )
    initial = await run_turn(agent, "参照元別セッション数とUUを比較して", "initial")
    assert initial[-1]["data"]["status"] == "complete"
    changed = await run_turn(
        agent,
        "今の結果をもとに2026年7月と2026年8月の参照元別セッション数を比較して",
        "different-periods",
    )
    assert changed[-1]["data"]["status"] == "partial"
    assert changed[-1]["data"]["stop_reason"] == "incomplete_requested_analysis"
    assert len(executor.invocations) == 1


@pytest.mark.asyncio
async def test_revocation_blocks_followup_before_reusing_saved_evidence(
    repository: InMemoryRepository,
) -> None:
    agent, director, executor = await configure_continuation(
        repository, [query(comparison_plan()), finish(), finish()]
    )
    initial = await run_turn(agent, "参照元別のUUを比較して", "before-revocation")
    assert initial[-1]["data"]["status"] == "complete"
    calls_before = len(director.contexts)
    del repository.grants[("user-a", "company_a_web")]
    with pytest.raises(AccessDenied):
        await run_turn(agent, "今の参照元の結果を説明してください", "after-revocation")
    assert len(director.contexts) == calls_before
    assert len(executor.invocations) == 1


@pytest.mark.asyncio
async def test_automatic_review_does_not_leave_last_auxiliary_axis_as_user_focus(
    repository: InMemoryRepository,
) -> None:
    agent, director, executor = await configure_review(repository)
    initial = await run_turn(agent, QUESTION, "review-before-followup")
    assert initial[-1]["data"]["status"] == "complete"
    assert executor.invocations[-1].arguments["dimensions"] == ["pagePath"]
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert set(state.metrics) == {"page_views", "active_users"}
    assert state.dimensions == ()
    assert state.period == DatePeriod(start=date(2026, 1, 1), end=date(2026, 6, 30))
    assert state.dataset_ids and state.findings

    director.decisions = [
        query(IntentPlan(intent="全体をモバイルに限定", device_term="mobile")),
        finish(),
    ]
    followup = await run_turn(agent, "その中でモバイルだけ", "review-followup")
    assert followup[-1]["data"]["status"] == "complete"
    assert executor.invocations[-1].arguments["dimensions"] == []
    assert set(executor.invocations[-1].arguments["metrics"]) == {
        "screenPageViews",
        "activeUsers",
    }
    latest = (await agent.session("user-a", "as_orchestrator")).state
    assert latest.focus.device == "mobile"
    assert set(state.dataset_ids).issubset(latest.dataset_ids)
    assert {f.id for f in state.findings}.issubset({f.id for f in latest.findings})


@pytest.mark.parametrize(
    "question",
    [
        "今の結果をモバイルだけに絞って説明して",
        "今の結果からOrganicの傾向を説明して",
        "今の結果を前月と比較して説明して",
        "今の結果を2026年7月に変更して説明して",
        "今の結果の日別の傾向を説明して",
        "今の結果からフィルターを解除して説明して",
        "今の結果を再取得して説明して",
        "ランディングページを分析して",
    ],
)
def test_discussion_reuse_never_bypasses_requested_scope_change(
    repository: InMemoryRepository, question: str
) -> None:
    definitions = list(repository.semantic_definitions.values())
    assert not existing_result_discussion(question, definitions)
    assert existing_result_discussion(
        "今のランディングページの結果を説明してください", definitions
    )


def test_reuse_requires_exact_scope_and_authorized_observations() -> None:
    plan = comparison_plan()
    device = AnalysisFilter(
        semantic_name="mobile",
        kind="device",
        field_name="deviceCategory",
        value="mobile",
    )
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
        period=plan.period,
        comparison_period=plan.comparison_period,
        metrics=("sessions", "active_users"),
        dimensions=("session_source",),
        filters=(device,),
    )
    good = ReportSection(
        title="参照元比較",
        message_id="same-scope",
        operation_id="old-operation",
        finding_ids=("authorized-finding",),
        evidence_ids=("authorized-evidence",),
        dimension_fields=("sessionSource",),
        metric_fields=("sessions", "activeUsers"),
        period=state.period,
        comparison_period=state.comparison_period,
        filters=(device,),
        observed_periods=("current", "comparison"),
        observations=({"sessionSource": "google", "sessions": 100},),
    )
    wrong = [
        {"period": plan.comparison_period},
        {"comparison_period": None},
        {"filters": ()},
        {"observed_periods": ("current",)},
        {"finding_ids": ("different-user-finding",)},
        {"observations": ()},
        {"evidence_ids": ()},
    ]
    workspace = AgentWorkspace(
        operation_id="new-operation",
        sections=(
            good,
            *(
                good.model_copy(update={"message_id": f"wrong-{index}", **changes})
                for index, changes in enumerate(wrong)
            ),
        ),
    )
    assert reusable_sections(workspace, state, {"authorized-finding"}) == (
        "same-scope",
    )
    assert reusable_sections(workspace, state, set()) == ()


def test_long_conversation_context_is_compact_without_dropping_verified_axes() -> None:
    workspace = AgentWorkspace(
        turns=tuple(
            {"role": "user" if index % 2 == 0 else "assistant", "content": "文" * 4000}
            for index in range(20)
        ),
        questions=("次に参照元を確認しますか？",),
        sections=tuple(
            ReportSection(
                title="ページ分析",
                operation_id="older-turn",
                message_id=f"msg-{index}",
                finding_ids=(f"finding-{index}",),
                evidence_ids=(f"evidence-{index}",),
                facts=("既に計算された長い文章" * 100,),
                dimension_fields=("landingPage",),
                metric_fields=("activeUsers",),
                observations=({"landingPage": "/articles", "activeUsers": 120},),
            )
            for index in range(16)
        ),
    )
    authorized = {f"finding-{index}" for index in range(4, 16)}
    context = planning_workspace(workspace, authorized)
    assert len(context["turns"]) == 4
    assert all(len(turn["content"]) <= 1000 for turn in context["turns"])
    assert context["history_truncated"]
    assert context["questions"] == ["次に参照元を確認しますか？"]
    assert len(context["sections"]) <= 8
    assert all("facts" not in section for section in context["sections"])
    assert all(
        set(section["finding_ids"]).issubset(authorized)
        for section in context["sections"]
    )
    assert context["sections"][-1]["observations"][0]["landingPage"] == "/articles"


@pytest.mark.asyncio
async def test_filter_change_cannot_finish_with_unfiltered_prior_evidence(
    repository: InMemoryRepository,
) -> None:
    agent, _, executor = await configure_continuation(
        repository, [query(comparison_plan()), finish(), finish(), finish()]
    )
    await run_turn(agent, "参照元の比較", "unfiltered-first")
    result = await run_turn(
        agent, "今の結果をモバイルだけにして説明", "needs-mobile-evidence"
    )
    assert result[-1]["data"]["status"] == "partial"
    assert result[-1]["data"]["stop_reason"] == "incomplete_requested_analysis"
    assert not result[-1]["data"]["reused_section_ids"]
    assert (
        "指定した絞込み条件"
        in (await repository.list_messages("as_orchestrator"))[-1].content
    )
    assert len(executor.invocations) == 1


@pytest.mark.asyncio
async def test_failed_primary_review_does_not_promote_auxiliary_trend_to_main_scope(
    repository: InMemoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, executor = await configure_review(repository)
    original_execute = executor.execute

    async def fail_first(invocation: Any) -> ToolExecutionResult:
        if not executor.invocations:
            executor.invocations.append(invocation)
            raise AnalysisOrchestrationError("ga4_service_unavailable")
        return await original_execute(invocation)

    monkeypatch.setattr(executor, "execute", fail_first)
    events = await run_turn(agent, QUESTION, "primary-failed")
    assert events[-1]["data"]["status"] == "partial"
    assert events[-1]["data"]["completed_perspectives"] == [
        "時系列",
        "流入チャネル",
        "主要ページ",
    ]
    state = (await agent.session("user-a", "as_orchestrator")).state
    assert state.dimensions == ()
    assert state.time_grain is None
    assert state.period == DatePeriod(start=date(2026, 1, 1), end=date(2026, 6, 30))
    assert state.comparison_period == DatePeriod(
        start=date(2025, 1, 1), end=date(2025, 6, 30)
    )
    assert state.dataset_ids and state.findings
