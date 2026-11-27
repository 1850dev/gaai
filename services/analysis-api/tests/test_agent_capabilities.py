from datetime import date

import pytest

from analytics_ai.agent.capabilities import capability_context, effective_definitions
from analytics_ai.agent.director import Ga4ToolCall
from analytics_ai.agent.models import AgentDecision, AgentWorkspace, ReportSection
from analytics_ai.agent.service import AgentService
from analytics_ai.domain.models import DatePeriod, SemanticDefinition, SemanticScope
from analytics_ai.orchestrator.planning import IntentPlan
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_agent import finish
from tests.test_agent_review import configure_review
from tests.test_orchestrator import active_users_plan, add_session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "term,label,field",
    [
        ("session_source", "参照元", "sessionSource"),
        ("page_path", "ページ", "pagePath"),
        ("device_category", "デバイス", "deviceCategory"),
        ("country", "国", "country"),
        ("gender", "性別", "userGender"),
        ("event", "イベント", "eventName"),
        ("browser", "ブラウザ", "browser"),
    ],
)
async def test_every_axis_requires_evidence_even_in_generic_workflow(
    repository: InMemoryRepository, term: str, label: str, field: str
) -> None:
    agent, director, executor = await configure_review(repository)
    # Override an existing canonical definition rather than making it ambiguous.
    for key, definition in list(repository.semantic_definitions.items()):
        if definition.canonical_name == term:
            del repository.semantic_definitions[key]
    repository.semantic_definitions["requested-axis"] = SemanticDefinition(
        id="requested-axis",
        canonical_name=term,
        aliases=(label,),
        kind="dimension",
        scope=SemanticScope.GLOBAL,
        version="a-web-v1",
        ga4_mapping={"dimension": field},
    )
    plan = active_users_plan()
    initial = AgentDecision(
        action="query",
        objective="分析",
        explanation="調査します。",
        workflow_id="analysis.exploration.v1",
        plan=plan,
    )
    director.decisions = [
        initial,
        finish(),
        initial.model_copy(
            update={"plan": plan.model_copy(update={"dimension_terms": (term,)})}
        ),
        finish(),
    ]
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id="as_orchestrator",
            content=f"2026年5月と2026年6月の{label}別のアクティブユーザーを比較して",
            operation_id="broad-coverage",
        )
    ]
    assert events[-1]["event"] == "done", events[-1]
    assert events[-1]["data"]["status"] == "complete"
    assert len(executor.invocations) == 2
    assert executor.invocations[-1].arguments["dimensions"] == [field]
    assert "requested_analysis_not_observed" in director.contexts[2]["validation_error"]
    assert "analysis_capabilities" in director.contexts[0]
    assert (
        executor.invocations[-1].arguments["date_ranges"][1]["end_date"] == "2026-05-31"
    )


def test_coverage_rejects_wrong_metric_period_and_empty_data() -> None:
    current = DatePeriod(start=date(2026, 6, 1), end=date(2026, 6, 30))
    previous = DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31))
    section = ReportSection(
        title="参照元",
        message_id="msg",
        finding_ids=("finding",),
        evidence_ids=("evidence",),
        metric_fields=("sessions",),
        dimension_fields=("sessionSource",),
        period=current,
        comparison_period=previous,
        operation_id="op",
        observations=({"sessionSource": "google", "sessions": 10},),
        observed_periods=("current", "comparison"),
    )
    workspace = AgentWorkspace(
        operation_id="op",
        required_dimension_fields=("sessionSource",),
        required_metric_fields=("activeUsers",),
        required_period=current,
        required_comparison_period=previous,
        sections=(section,),
    )
    assert AgentService.missing_fields(workspace) == ("activeUsers",)
    # A breakdown of a different metric plus an overall requested metric is
    # not evidence of the requested axis/metric combination.
    overall = section.model_copy(
        update={
            "metric_fields": ("activeUsers",),
            "dimension_fields": (),
            "observations": ({"activeUsers": 100},),
        }
    )
    assert AgentService.missing_fields(
        workspace.model_copy(
            update={
                "sections": (section, overall),
            }
        )
    ) == ("sessionSource × activeUsers",)
    for updates in (
        {"observed_periods": ("current",)},
        {"period": previous},
        {"observations": ()},
        {"operation_id": "old"},
    ):
        gaps = AgentService.missing_fields(
            workspace.model_copy(
                update={"sections": (section.model_copy(update=updates),)}
            )
        )
        assert "sessionSource" in gaps
        assert "指定した両期間の比較" in gaps


@pytest.mark.asyncio
async def test_catalog_respects_unavailable_lower_scope(
    repository: InMemoryRepository,
) -> None:
    session = await add_session(repository)
    original = repository.semantic_definitions["global-active-users"]
    blocked = original.model_copy(
        update={
            "id": "blocked",
            "scope": SemanticScope.DATA_SOURCE_SET,
            "company_id": session.company_id,
            "data_source_set_id": session.data_source_set_id,
            "available": False,
        }
    )
    definitions = effective_definitions([original, blocked], session)
    assert definitions == []
    catalog = capability_context(definitions)
    assert catalog["metric_terms"] == []
    assert catalog["methods"]["funnel"]["registered_funnels"] == []
    assert all(
        p["status"] == "missing_registered_definition" for p in catalog["perspectives"]
    )
    assert any(
        p["feature"] == "cohort_retention_and_server_pivot"
        for p in catalog["unavailable"]
    )


def test_funnel_has_its_own_input_contract() -> None:
    result = Ga4ToolCall(
        plan=IntentPlan(
            intent="登録済みファネル",
            skill_id=AnalysisSkillId.FUNNEL,
            funnel_term="reviewed_checkout",
            metric_terms=(),
        ),
        observed_filters=(),
    )
    assert result.plan.metric_terms == ()
    with pytest.raises(ValueError, match="ga4_query_requires"):
        Ga4ToolCall(
            plan=IntentPlan(intent="未登録のファネル", skill_id=AnalysisSkillId.FUNNEL),
            observed_filters=(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,expected",
    [
        ("月別のUU推移", "yearMonth"),
        ("日別のUU", "date"),
        ("週次のUU", "yearWeek"),
        ("年別のUU", "year"),
        ("active users by month", "yearMonth"),
    ],
)
async def test_time_grain_is_a_real_capability_and_completion_requirement(
    repository: InMemoryRepository, question: str, expected: str
) -> None:
    from analytics_ai.agent.workflows import explicit_dimension_fields
    from analytics_ai.ga4.chat_sessions import Ga4ChatSessionService

    session = await add_session(repository)
    source = await repository.get_data_source_set(session.data_source_set_id)
    assert source is not None
    definitions = effective_definitions(
        list(Ga4ChatSessionService._semantic_definitions(source)), session
    )
    assert explicit_dimension_fields(question, definitions) == (expected,)
    time = next(
        p for p in capability_context(definitions)["perspectives"] if p["id"] == "time"
    )
    assert time["status"] == "available_subject_to_compatibility"
    assert "year_month" in time["dimension_terms"]
    wrong = ReportSection(
        title="wrong grain",
        message_id="msg",
        finding_ids=("f",),
        evidence_ids=("e",),
        dimension_fields=(),
        metric_fields=("activeUsers",),
        observations=({"activeUsers": 100},),
        operation_id="op",
    )
    workspace = AgentWorkspace(
        operation_id="op",
        required_dimension_fields=(expected,),
        required_metric_fields=("activeUsers",),
        sections=(wrong,),
    )
    assert expected in AgentService.missing_fields(workspace)
