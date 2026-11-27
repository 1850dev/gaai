"""Opt-in real Gemini planning evaluation; all analytics/storage are synthetic.

Run in the local API container via stdin with --live. Never import api.app,
Firestore, OAuth storage or a real analytics executor. No product writes occur.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from time import monotonic
from typing import Any

from analytics_ai.agent.director import VertexAgentDirector
from analytics_ai.agent.models import AgentDecision, AgentSummary, GroundedAssessment
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.analysis_state.reducer import AnalysisStateReducer
from analytics_ai.authorization.service import AuthorizationService
from analytics_ai.cache.store import (
    DatasetCache,
    InMemoryCacheBlobs,
    InMemoryCacheMetadata,
)
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    AnalysisFilter,
    AnalysisSession,
    AnalysisState,
    CompanyMember,
    DataSourceSet,
    DatePeriod,
    Ga4AuthMode,
    Ga4Platform,
    Ga4Source,
    Ga4StreamScope,
    User,
    UserAccessGrant,
)
from analytics_ai.ga4.chat_sessions import Ga4ChatSessionService
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import McpGateway, PreparedInvocation
from analytics_ai.orchestrator.planning import IntentPlan, PlanCompiler, TimeGrain
from analytics_ai.orchestrator.service import AnalysisOrchestrator
from analytics_ai.repositories.memory import InMemoryRepository

FIELDS = (
    "period",
    "comparison_period",
    "metrics",
    "dimensions",
    "time_grain",
    "filters",
)
MAY = DatePeriod(start="2026-05-01", end="2026-05-31")
JUNE = DatePeriod(start="2026-06-01", end="2026-06-30")
APRIL = DatePeriod(start="2026-04-01", end="2026-04-30")


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def mismatch(actual: AnalysisState, expected: AnalysisState) -> list[str]:
    return [
        f
        for f in FIELDS
        if (
            set(actual.filters) != set(expected.filters)
            if f == "filters"
            else getattr(actual, f) != getattr(expected, f)
        )
    ]


class SyntheticExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult:
        assert invocation.tool_name == "run_report"
        assert invocation.data_source_set_id == "qa_synthetic_web"
        self.calls += 1
        rows = []
        for index, period in enumerate(invocation.arguments["date_ranges"]):
            day = period["start_date"].replace("-", "")
            values = {
                "sessionSource": "example.test",
                "yearMonth": day[:6],
                "date": day,
                "landingPage": "/example",
                "landingPagePlusQueryString": "/example",
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


class SeedDirector:
    def __init__(self) -> None:
        self.seeded = False

    async def decide(self, context: dict[str, Any]) -> AgentDecision:
        if self.seeded:
            return AgentDecision(
                action="finish",
                objective="合成分析",
                explanation="合成データを確認しました。",
            )
        self.seeded = True
        return AgentDecision(
            action="query",
            objective="合成分析",
            explanation="合成データを確認します。",
            plan=IntentPlan(
                intent="月別参照元比較",
                period=JUNE,
                comparison_period=MAY,
                metric_terms=("sessions", "active_users"),
                dimension_terms=("session_source",),
                time_grain=TimeGrain.MONTH,
            ),
        )

    async def summarize(self, context: dict[str, Any]) -> AgentSummary:
        return AgentSummary(
            explanation="計画検証用の合成結果です。回答文章の品質は評価対象外です。",
            assessments=(
                GroundedAssessment(
                    interpretation="合成の根拠を確認しました。",
                    fact_ids=tuple(f["id"] for f in context["facts"][:8]),
                ),
            )
            if context["facts"]
            else (),
        )


class ForbiddenPlanner:
    async def plan(self, **kwargs: Any) -> IntentPlan:
        raise AssertionError("only_agent_trusted_plans_are_allowed")


class Budget:
    def __init__(self, maximum: int = 64) -> None:
        self.maximum = maximum
        self.calls = 0
        self.tokens = 0

    def bind(self, director: VertexAgentDirector) -> Any:
        client = director.client
        original = client.aio.models.generate_content

        async def generate(**kwargs: Any) -> Any:
            if self.calls >= self.maximum:
                raise RuntimeError("evaluation_model_call_budget_exhausted")
            self.calls += 1
            response = await original(**kwargs)
            self.tokens += getattr(response.usage_metadata, "total_token_count", 0) or 0
            return response

        # Keep the actual SDK client and its close/context lifecycle intact.
        models: Any = client.aio.models
        models.generate_content = generate
        return client


class MeasuredDirector(SeedDirector):
    def __init__(
        self, live: VertexAgentDirector, repository: InMemoryRepository
    ) -> None:
        self.live = live
        self.repository = repository
        self.expected: AnalysisState | None = None
        self.decisions: list[dict[str, Any]] = []

    async def decide(self, context: dict[str, Any]) -> AgentDecision:
        started = monotonic()
        try:
            decision = await self.live.decide(context)
        except Exception as error:
            self.decisions.append({"error_type": type(error).__name__})
            raise
        row: dict[str, Any] = {
            "action": decision.action,
            "workflow": decision.workflow_id,
            "latency_seconds": round(monotonic() - started, 2),
            "received_validation_feedback": bool(context.get("validation_error")),
            "contract": (context.get("continuation_contract") or {}).get("id"),
        }
        if decision.plan and self.expected is not None:
            session = await self.repository.get_session("as_synthetic_eval")
            assert session is not None
            state = session.state
            scope = await AuthorizationService(self.repository).authorize_source_set(
                "qa-user", "qa_synthetic_web"
            )
            try:
                compiled = PlanCompiler().compile(
                    decision.plan,
                    state=state,
                    scope=scope,
                    definitions=list(self.repository.semantic_definitions.values()),
                )
                effective = AnalysisStateReducer().apply(state, compiled.delta).state
                row["raw_mismatch"] = mismatch(effective, self.expected)
                row["raw_conditions"] = effective.model_dump(
                    mode="json", include=set(FIELDS)
                )
            except ValueError as error:
                row["raw_compile_error"] = str(error)[:160]
        self.decisions.append(row)
        return decision


async def build() -> tuple[AgentService, InMemoryRepository, SyntheticExecutor]:
    source = DataSourceSet(
        id="qa_synthetic_web",
        company_id="qa_company",
        name="合成サイト",
        semantic_version="ga4-oauth-v1",
        enabled=True,
        ga4=Ga4Source(
            display_name="合成サイト",
            property_id="222222222",
            auth_mode=Ga4AuthMode.USER_OAUTH,
            stream_scopes=(
                Ga4StreamScope(stream_id="3333333333", platform=Ga4Platform.WEB),
            ),
        ),
    )
    repository = InMemoryRepository(
        users=[User(uid="qa-user", email="qa@example.test")],
        members=[CompanyMember(user_id="qa-user", company_id="qa_company")],
        source_sets=[source],
        grants=[
            UserAccessGrant(
                id="qa-grant", user_id="qa-user", data_source_set_id=source.id
            )
        ],
        semantic_definitions=list(Ga4ChatSessionService._semantic_definitions(source)),
    )
    await repository.create_session(
        AnalysisSession(
            id="as_synthetic_eval",
            user_id="qa-user",
            company_id=source.company_id,
            data_source_set_id=source.id,
            state=AnalysisState(
                data_source_set_id=source.id, semantic_version=source.semantic_version
            ),
        )
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        repository_backend="memory",
        cache_backend="memory",
        enable_ga4_user_oauth=False,
    )
    executor = SyntheticExecutor()
    orchestrator = AnalysisOrchestrator(
        repository,
        planner=ForbiddenPlanner(),
        gateway=McpGateway(settings, repository),
        executor=executor,
        cache=DatasetCache(InMemoryCacheMetadata(), InMemoryCacheBlobs()),
    )
    return (
        AgentService(repository, MemoryAgentStore(), SeedDirector(), orchestrator),
        repository,
        executor,
    )


async def turn(
    agent: AgentService, question: str, operation: str
) -> list[dict[str, Any]]:
    return [
        event
        async for event in agent.turn(
            user_id="qa-user",
            session_id="as_synthetic_eval",
            content=question,
            operation_id=operation,
        )
    ]


async def evaluate(repetitions: int, live_enabled: bool) -> None:
    settings = Settings()
    if live_enabled and settings.environment not in {"local", "dev"}:
        raise RuntimeError("live_evaluation_requires_local_or_dev")
    budget = Budget()
    outcomes = []
    live = VertexAgentDirector(settings) if live_enabled else None
    client = budget.bind(live) if live else None
    emit(
        {
            "kind": "start",
            "model": live.model if live else "self-test",
            "repetitions": repetitions,
            "max_model_calls": budget.maximum,
            "deadline_seconds": 600,
            "analytics": "synthetic",
            "storage": "memory",
            "summarization": "synthetic_not_scored",
        }
    )
    try:
        for repeat in range(repetitions):
            agent, repository, executor = await build()
            seeded = await turn(agent, "月別に参照元をセッション数とUUで比較", "seed")
            assert seeded[-1]["data"]["status"] == "complete"
            session = await repository.get_session("as_synthetic_eval")
            assert session is not None
            expected = session.state
            if not live:
                emit(
                    {
                        "kind": "self_test",
                        "seed": "complete",
                        "synthetic_calls": executor.calls,
                    }
                )
                return
            measured = MeasuredDirector(live, repository)
            agent.director = measured
            organic = AnalysisFilter(
                semantic_name="organic_search",
                kind="channel",
                field_name="sessionDefaultChannelGroup",
                value="Organic Search",
            )
            mobile = AnalysisFilter(
                semantic_name="mobile",
                kind="device",
                field_name="deviceCategory",
                value="mobile",
            )
            cases: list[tuple[str, str, dict[str, Any]]] = [
                ("organic", "Organicだけ", {"filters": (organic,)}),
                ("mobile", "その中でモバイルだけ", {"filters": (organic, mobile)}),
                (
                    "baseline",
                    "比較基準だけ2026年4月に変更してください",
                    {"comparison_period": APRIL},
                ),
                (
                    "axis",
                    "参照元からランディングページに変更してください",
                    {"dimensions": ("year_month", "landing_page")},
                ),
                (
                    "grain",
                    "月別から日別に変更してください",
                    {"dimensions": ("date", "landing_page"), "time_grain": "day"},
                ),
                (
                    "clear_comparison",
                    "比較だけ解除してください",
                    {"comparison_period": None},
                ),
                ("ambiguous", "日別か月別のどちらかに変更して", {}),
                (
                    "no_fetch",
                    "新しいデータ取得はせず、直前の取得結果と条件を説明してください。レポートは作成しないでください。",
                    {},
                ),
            ]
            for label, question, update in cases:
                if budget.calls >= budget.maximum:
                    raise RuntimeError("evaluation_model_call_budget_exhausted")
                expected = expected.model_copy(update=update)
                measured.expected = expected
                measured.decisions = []
                calls_before, model_before = executor.calls, budget.calls
                started = monotonic()
                events = await turn(agent, question, f"eval-{repeat}-{label}")
                session = await repository.get_session("as_synthetic_eval")
                assert session is not None
                state = session.state
                workspace = await agent.workspace("qa-user", "as_synthetic_eval")
                status = events[-1]["data"].get("status")
                differences = mismatch(state, expected)
                safe_case = label in {"ambiguous", "no_fetch"}
                fetched = executor.calls - calls_before
                passed = not differences and not workspace.report_requested
                if safe_case:
                    passed = passed and fetched == 0
                else:
                    passed = passed and status == "complete" and fetched == 1
                if label == "no_fetch":
                    passed = passed and status == "complete"
                if label == "ambiguous":
                    passed = (
                        passed
                        and status == "waiting"
                        and bool(events[-1]["data"].get("questions"))
                    )
                row = {
                    "kind": "case",
                    "repeat": repeat + 1,
                    "case": label,
                    "passed": passed,
                    "status": status,
                    "state_mismatch": differences,
                    "questions_returned": bool(events[-1]["data"].get("questions")),
                    "synthetic_calls": fetched,
                    "model_calls": budget.calls - model_before,
                    "latency_seconds": round(monotonic() - started, 2),
                    "errors": workspace.execution_errors,
                    "stop_reason": workspace.stop_reason,
                    "decisions": measured.decisions,
                }
                emit(row)
                outcomes.append(row)
                if not passed:
                    emit(
                        {
                            "kind": "chain_stopped",
                            "repeat": repeat + 1,
                            "reason": "avoid_scoring_followups_against_failed_anchor",
                        }
                    )
                    break
    finally:
        if client:
            await client.aio.aclose()
        emit(
            {
                "kind": "summary",
                "cases": len(outcomes),
                "passed": sum(r["passed"] for r in outcomes),
                "model_calls": budget.calls,
                "reported_tokens": budget.tokens,
                "real_ga4_calls": 0,
                "persistent_writes": 0,
            }
        )
    if live_enabled and (
        len(outcomes) != repetitions * 8 or not all(r["passed"] for r in outcomes)
    ):
        raise RuntimeError("evaluation_incomplete_or_failed")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--repetitions", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    try:
        async with asyncio.timeout(600):
            await evaluate(args.repetitions, args.live)
    except Exception as error:
        emit({"kind": "evaluation_error", "error_type": type(error).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
