"""Joint queries, persisted cells and grounded answers across GA4 perspectives."""

from datetime import date

import pytest

from analytics_ai.agent.facts import finding_context
from analytics_ai.agent.markdown import text
from analytics_ai.agent.models import AgentDecision, AgentWorkspace, ReportSection
from analytics_ai.agent.presentation import render_summary
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import MemoryAgentStore
from analytics_ai.agent.workflows import joint_dimension_fields
from analytics_ai.domain.models import CalculationKind, DatePeriod
from analytics_ai.ga4.chat_sessions import Ga4ChatSessionService
from analytics_ai.orchestrator.category_comparison import category_values
from analytics_ai.orchestrator.planning import IntentPlan, RankingIntent, TimeGrain
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import Director, finish
from tests.test_orchestrator import add_session, build_orchestrator


def register(repository: InMemoryRepository) -> dict[str, str]:
    definitions = Ga4ChatSessionService._semantic_definitions(
        repository.source_sets["company_a_web"]
    )
    repository.semantic_definitions = {d.id: d for d in definitions}
    return {
        d.canonical_name: d.ga4_mapping.get("dimension", d.ga4_mapping.get("metric"))
        for d in definitions
        if d.kind in {"dimension", "date", "metric"} and d.ga4_mapping
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("comparison", [False, True])
@pytest.mark.parametrize(
    "dimensions,metrics",
    [
        (("page_path", "session_source"), ("page_views",)),
        (("landing_page", "session_source_medium"), ("sessions", "active_users")),
        (("session_channel_group", "device_category", "page_path"), ("page_views",)),
        (("country", "browser"), ("active_users",)),
        (("event_name", "device_category"), ("event_count",)),
        (("year_month", "session_channel_group"), ("active_users", "engagement_rate")),
        (
            ("page_path", "session_source", "device_category", "country"),
            ("page_views", "sessions", "active_users", "engagement_rate"),
        ),
    ],
)
async def test_cross_cells_survive_execution_persistence_answer_and_cache(
    repository: InMemoryRepository,
    dimensions: tuple[str, ...],
    metrics: tuple[str, ...],
    comparison: bool,
) -> None:
    session = await add_session(repository)
    fields = register(repository)
    rows = [
        {
            **{fields[d]: f"observed-{d}-{i}" for d in dimensions},
            **{fields[m]: str(100 - i * 30 - p * 10) for m in metrics},
            "dateRange": "current" if p == 0 else "comparison",
        }
        for p in range(2 if comparison else 1)
        for i in range(2)
    ]
    if "year_month" in dimensions:
        for row in rows:
            row["yearMonth"] = "202606" if row["dateRange"] == "current" else "202605"
    plan = IntentPlan(
        intent="登録済み項目の複合集計",
        skill_id=AnalysisSkillId.CROSS_BREAKDOWN,
        period=DatePeriod(start=date(2026, 6, 1), end=date(2026, 6, 30)),
        comparison_period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))
        if comparison
        else None,
        dimension_terms=dimensions,
        metric_terms=metrics,
        ranking=RankingIntent(limit=10),
    )
    orchestrator, executor = build_orchestrator(repository, [], {"rows": rows})
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id=session.id,
        content=plan.intent,
        request_id="cross-1",
        trusted_plan=plan,
    )
    assert executor.invocations[0].arguments["property_id"] == "222222222"
    assert executor.invocations[0].arguments["dimensions"] == [
        fields[d] for d in dimensions
    ]
    assert result.visualization.type == "table"
    assert result.visualization.dimensions == tuple(fields[d] for d in dimensions)
    for finding in result.findings:
        restored = await repository.get_finding(finding.id)
        assert restored.category_values == finding.category_values
        assert len(finding.category_values) == 2
        assert set(finding.category_values[0].dimensions) == {
            fields[d] for d in dimensions
        }
        assert finding.category_values[0].value == "100"
        assert finding.value == "100"  # not a sum over dates/categories
        assert not any(
            c.kind in {CalculationKind.PERIOD_TOTAL, CalculationKind.PERIOD_AVERAGE}
            for c in finding.calculations
        )
        if comparison and "year_month" in dimensions:
            assert not finding.category_comparisons
            assert finding.comparison_value is None
        elif comparison:
            assert len(finding.category_comparisons[0].dimensions) == len(dimensions)
        context = finding_context(finding, list(finding.evidence_ids))
        body = render_summary(None, [context], (), ())
        assert text(rows[0][fields[dimensions[-1]]]) in body
        assert "根拠:" in body
    cached = await orchestrator.analyze(
        user_id="user-a",
        session_id=session.id,
        content=plan.intent,
        request_id="cross-2",
        trusted_plan=plan,
    )
    assert cached.cache_hit and len(executor.invocations) == 1
    assert cached.findings[0].category_values == result.findings[0].category_values


@pytest.mark.asyncio
async def test_date_channel_report_requests_chronological_rows_within_policy(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    register(repository)
    orchestrator, executor = build_orchestrator(
        repository,
        [],
        {
            "rows": [
                {
                    "date": "20260502",
                    "sessionDefaultChannelGroup": "Direct",
                    "screenPageViews": "10",
                },
            ]
        },
    )
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id=session.id,
        content="五月の日別・チャネル別のページビュー",
        request_id="channel-table",
        trusted_plan=IntentPlan(
            intent="日別・チャネル別",
            skill_id=AnalysisSkillId.CROSS_BREAKDOWN,
            period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
            time_grain=TimeGrain.DAY,
            dimension_terms=("session_channel_group",),
            metric_terms=("page_views",),
            preferred_chart_type="table",
        ),
    )
    invocation = executor.invocations[0]
    assert invocation.arguments["limit"] == 1000
    assert invocation.arguments["order_bys"] == [
        {"dimension": {"dimension_name": "date"}, "desc": False},
        {"dimension": {"dimension_name": "sessionDefaultChannelGroup"}, "desc": False},
    ]
    assert result.visualization.dimensions == ("date", "sessionDefaultChannelGroup")


def test_separate_marginals_do_not_satisfy_joint_evidence() -> None:
    def section(axes: tuple[str, ...]) -> ReportSection:
        return ReportSection(
            title="確認",
            message_id=str(axes),
            operation_id="op",
            finding_ids=("finding",),
            evidence_ids=("evidence",),
            dimension_fields=axes,
            metric_fields=("screenPageViews",),
            observations=({**{a: "value" for a in axes}, "screenPageViews": 10},),
        )

    state = AgentWorkspace(
        operation_id="op",
        required_dimension_fields=("pagePath", "sessionSource"),
        required_metric_fields=("screenPageViews",),
        required_dimension_groups=(("pagePath", "sessionSource"),),
        sections=(section(("pagePath",)), section(("sessionSource",))),
    )
    assert AgentService.missing_fields(state) == ("pagePath × sessionSource",)
    complete = state.model_copy(
        update={"sections": (section(("pagePath", "sessionSource")),)}
    )
    assert not AgentService.missing_fields(complete)
    wrong_metric = complete.model_copy(
        update={"required_metric_fields": ("activeUsers",)}
    )
    assert "pagePath × sessionSource × activeUsers" in AgentService.missing_fields(
        wrong_metric
    )
    missing_cells = complete.model_copy(
        update={
            "sections": (
                complete.sections[0].model_copy(
                    update={
                        "observations": ({"pagePath": "/a", "screenPageViews": 10},)
                    }
                ),
            )
        }
    )
    assert "pagePath × sessionSource" in AgentService.missing_fields(missing_cells)


@pytest.mark.asyncio
async def test_partial_page_plan_is_compiled_as_requested_cross_and_explained(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    register(repository)
    plan = IntentPlan(
        intent="ページのPV",
        dimension_terms=("page_path",),
        metric_terms=("page_views",),
        period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    rows = [
        {
            "pagePathPlusQueryString": "/same",
            "sessionSource": "google",
            "screenPageViews": 90,
        },
        {
            "pagePathPlusQueryString": "/same",
            "sessionSource": "newsletter",
            "screenPageViews": 20,
        },
    ]
    orchestrator, executor = build_orchestrator(repository, [], {"rows": rows})
    director = Director(
        [
            AgentDecision(
                action="query", objective="複合集計", explanation="確認", plan=plan
            ),
            finish(),
        ]
    )
    agent = AgentService(repository, MemoryAgentStore(), director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=session.id,
            content="ページと参照元を掛け合わせたPVを確認してください",
            operation_id="cross-agent",
        )
    ]
    assert events[-1]["data"]["status"] == "complete"
    assert executor.invocations[0].arguments["dimensions"] == [
        "pagePathPlusQueryString",
        "sessionSource",
    ]
    fact = director.contexts[-1]["facts"][0]
    assert fact["category_values"][1]["dimensions"]["sessionSource"] == "newsletter"
    # Read-only compatibility for findings saved before joint-cell persistence.
    for key, finding in repository.findings.items():
        repository.findings[key] = finding.model_copy(
            update={"dimension_fields": (), "category_values": ()}
        )
    workspace = await agent.workspace("user-a", session.id)
    restored = await agent.facts(await agent.session("user-a", session.id), workspace)
    assert (
        restored[0]["category_values"][1]["dimensions"]["sessionSource"] == "newsletter"
    )


def test_cross_sample_never_sums_duplicates_or_truncates_identity() -> None:
    rows = [
        {"page": "/a", "source": "x", "users": 80},
        {"page": "/a", "source": "y", "users": 70},
        {"page": "/a", "source": "x", "users": 20},
        {"page": "/missing", "users": 10},
        {"page": "/invalid", "source": "z", "users": "NaN"},
        {"page": "p" * 501, "source": "z", "users": 20},
    ]
    cells = category_values(rows, fields=("page", "source"), metric="users")
    assert len(cells) == 1
    assert cells[0].dimensions == {"page": "/a", "source": "y"}
    assert cells[0].value == "70"


def test_cross_patterns_do_not_collapse_independent_analyses(
    repository: InMemoryRepository,
) -> None:
    register(repository)
    definitions = list(repository.semantic_definitions.values())
    assert joint_dimension_fields("ページと参照元を掛け合わせて", definitions)
    assert not joint_dimension_fields("ページと参照元を別々に分析", definitions)
    assert len(joint_dimension_fields("月別とチャネルをクロス集計", definitions)) == 2


@pytest.mark.asyncio
async def test_date_category_auto_selects_joint_skill_not_one_axis_trend(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    register(repository)
    orchestrator, executor = build_orchestrator(
        repository,
        [],
        {
            "rows": [
                {
                    "yearMonth": "202606",
                    "sessionDefaultChannelGroup": "Direct",
                    "activeUsers": 50,
                }
            ]
        },
    )
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id=session.id,
        content="月とチャネル",
        request_id="time-cross",
        trusted_plan=IntentPlan(
            intent="月とチャネル",
            time_grain=TimeGrain.MONTH,
            period=DatePeriod(start=date(2026, 6, 1), end=date(2026, 6, 30)),
            metric_terms=("active_users",),
            dimension_terms=("session_channel_group",),
        ),
    )
    assert len(executor.invocations) == 1
    assert result.findings[0].skill_id == AnalysisSkillId.CROSS_BREAKDOWN
    assert result.visualization.type == "table"
