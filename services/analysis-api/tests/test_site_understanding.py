"""Background is available at every stage but never becomes authority."""

import json

import pytest
from evaluation.live_continuation import build, turn
from evaluation.site_context import CaptureDirector

from analytics_ai.agent.director import (
    DIRECTOR_INSTRUCTIONS,
    SELECTION_INSTRUCTIONS,
    SUMMARY_INSTRUCTIONS,
    ga4_argument_context,
)
from analytics_ai.agent.models import AgentSummary, GroundedAssessment, SiteContext
from analytics_ai.agent.site_context import (
    SITE_UNDERSTANDING_INSTRUCTIONS,
    site_understanding,
)
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import CompanyMember, User, UserAccessGrant
from tests.test_agent import finish, proposal
from tests.test_director_continuation import query, selection, vertex
from tests.test_measurement_reasoning import evaluation


def context():
    return site_understanding(
        SiteContext(
            site_basics="店舗の来店案内サイト",
            goals="必要な案内への到達を支援",
            technical_constraints="共通CMSを使用",
            content_policy="案内を充実させたい",
            allowed_surfaces=(),
            allow_new_content=False,
            allow_rewrite=False,
        )
    )


def test_projection_is_background_preserves_text_and_does_not_mutate_storage():
    legacy = SiteContext(
        technical_constraints="長い背景" * 800,
        allowed_surfaces=(),
        allow_rewrite=False,
    )
    projected = site_understanding(legacy)
    assert projected["role"] == "background_not_policy"
    assert projected["technical_background"] == legacy.technical_constraints
    assert "allowed_surfaces" not in projected
    assert "technical_constraints" not in projected
    assert legacy.allowed_surfaces == () and not legacy.allow_rewrite
    assert SiteContext.model_validate(legacy.model_dump()) == legacy


@pytest.mark.parametrize(
    "instructions",
    [
        DIRECTOR_INSTRUCTIONS,
        SELECTION_INSTRUCTIONS,
        SUMMARY_INSTRUCTIONS,
    ],
)
def test_every_generation_stage_explains_background_not_policy(instructions):
    assert SITE_UNDERSTANDING_INSTRUCTIONS in instructions
    assert "CURRENT explicit request" in instructions
    assert "Missing site information must not block" in instructions


def test_argument_projection_keeps_site_but_does_not_invent_query_conditions():
    supplied = {
        "site_context": context(),
        "user_question": "今月のPVを確認",
        "analysis_state": {"metrics": ["page_views"], "filters": []},
        "unrelated_payload": "do not forward",
    }
    projected = ga4_argument_context(supplied)
    assert projected["site_context"] == supplied["site_context"]
    assert projected["analysis_state"] == supplied["analysis_state"]
    assert "unrelated_payload" not in projected
    assert "property_id" not in projected


@pytest.mark.asyncio
async def test_selection_and_query_receive_identical_background():
    director, calls = vertex([selection(), query(metric_terms=["page_views"])])
    result = await director.decide(
        {
            "user_question": "PVを見せて",
            "site_context": context(),
            "analysis_state": {"metrics": ["sessions"]},
        }
    )
    assert result.plan and result.plan.metric_terms == ("page_views",)
    assert len(calls) == 2
    for call in calls:
        assert json.loads(call["contents"])["site_context"] == context()
        assert SITE_UNDERSTANDING_INSTRUCTIONS in call["config"].system_instruction


@pytest.mark.asyncio
async def test_advice_and_summary_receive_background_without_proposal_bans():
    chosen = selection("evidence.answer")
    chosen["workflow_id"] = "advisory.site_improvement.v1"
    answer = finish().model_copy(
        update={"proposals": (proposal(content_action="new", evaluation=evaluation()),)}
    )
    summary = AgentSummary(
        explanation="来店案内の利用状況です。",
        assessments=(
            GroundedAssessment(
                interpretation="案内改善の仮説です。",
                fact_ids=("finding_ok",),
            ),
        ),
    )
    director, calls = vertex(
        [chosen, answer.model_dump(mode="json"), summary.model_dump(mode="json")]
    )
    supplied = {
        "user_question": "新しい案内ページの案を相談したい",
        "catalog": [{"term": "active_users", "kind": "metric"}],
        "site_context": context(),
        "facts": [{"id": "finding_ok", "evidence_ids": ["evidence_ok"]}],
    }
    decision = await director.decide(supplied)
    assert decision.proposals and decision.proposals[0].content_action == "new"
    assert await director.summarize(supplied) == summary
    assert len(calls) == 3
    for call in calls:
        assert json.loads(call["contents"])["site_context"] == context()
        assert SITE_UNDERSTANDING_INSTRUCTIONS in call["config"].system_instruction


@pytest.mark.asyncio
async def test_current_site_revision_is_loaded_each_turn_without_new_data():
    agent, _, executor = await build()
    capture = CaptureDirector()
    agent.director = capture
    saved = await agent.save_site(
        "qa-user", "qa_synthetic_web", SiteContext(site_basics="店舗情報")
    )
    await turn(agent, "分析の方針を相談", "first-context")
    assert capture.context["site_context"]["site_basics"] == "店舗情報"
    updated = await agent.save_site(
        "qa-user",
        "qa_synthetic_web",
        saved.model_copy(update={"site_basics": "資料案内"}),
    )
    await turn(agent, "分析の方針を相談", "second-context")
    assert capture.context["site_context"] == site_understanding(updated)
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_site_context_isolated_by_owner_and_set_even_for_same_property():
    agent, repository, _ = await build()
    await agent.save_site(
        "qa-user", "qa_synthetic_web", SiteContext(site_basics="個人の背景")
    )
    repository.users["other"] = User(uid="other", email="other@example.test")
    repository.members[("qa_company", "other")] = CompanyMember(
        user_id="other", company_id="qa_company"
    )
    with pytest.raises(AccessDenied):
        await agent.site("other", "qa_synthetic_web")
    repository.grants[("other", "qa_synthetic_web")] = UserAccessGrant(
        id="other-grant", user_id="other", data_source_set_id="qa_synthetic_web"
    )
    assert (await agent.site("other", "qa_synthetic_web")).site_basics == ""
    repository.source_sets["second_set"] = repository.source_sets[
        "qa_synthetic_web"
    ].model_copy(update={"id": "second_set"})
    repository.grants[("qa-user", "second_set")] = UserAccessGrant(
        id="second-grant", user_id="qa-user", data_source_set_id="second_set"
    )
    assert (await agent.site("qa-user", "second_set")).site_basics == ""
    del repository.grants[("qa-user", "qa_synthetic_web")]
    with pytest.raises(AccessDenied):
        await agent.site("qa-user", "qa_synthetic_web")
