"""Measurement provenance and proposal compatibility, without external requests."""

import json

import pytest
from evaluation.live_continuation import build, turn
from evaluation.site_context import CaptureDirector, PageExecutor
from pydantic import ValidationError

from analytics_ai.agent.director import SUMMARY_INSTRUCTIONS, AdvisoryDecision
from analytics_ai.agent.measurement import MEASUREMENT_INSTRUCTIONS, measurement_context
from analytics_ai.agent.models import (
    AgentRecord,
    AgentWorkspace,
    GoalCheck,
    Proposal,
    ProposalEvaluation,
    SiteContext,
)
from analytics_ai.agent.presentation import render_summary
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.store import _decode_record, _encode_record
from tests.test_agent import finish, proposal
from tests.test_agent_store import assert_firestore_values
from tests.test_director_continuation import query, selection, vertex


def evaluation():
    return ProposalEvaluation(
        goal_alignment="案内を探しやすくする仮説",
        metric_reason="閲覧の集中を確認する参考指標",
        metric_limitations="閲覧数だけでは案内への到達を測れない",
        measurement_status="needs_confirmation",
        prerequisites=("既存導線と計測方法を確認",),
        goal_checks=(
            GoalCheck(
                goal="案内を探しやすくする",
                required_evidence="目的情報を見つけられたかの確認",
                goal_basis="unconfirmed",
                goal_quote=None,
                evidence_status="goal_unconfirmed",
                gap="閲覧数だけでは目的情報の発見を判断できない",
                next_check="担当者と利用時の課題を確認する",
                next_check_kind="human_review",
            ),
        ),
    )


def test_answer_retains_evaluation_even_when_summary_is_unavailable():
    item = proposal(evaluation=evaluation()).model_copy(update={"success_metric": None})
    facts = [
        {
            "id": "f",
            "metric": "page_views",
            "text": "PV: 120",
            "evidence_ids": ["evidence_ok"],
        }
    ]
    rendered = render_summary(None, facts, (), (), proposals=(item,))
    assert "## 改善仮説の評価方法（未検証）" in rendered
    assert "評価指標候補: 未確定" in rendered
    assert evaluation().metric_limitations in rendered
    assert evaluation().prerequisites[0] in rendered
    assert "`evidence_ok`" in rendered
    with pytest.raises(ValueError, match="unknown_evidence"):
        render_summary(None, [], (), (), proposals=(item,))
    unsafe = item.model_copy(update={"title": "<script>**title**"})
    assert "<script>" not in render_summary(None, facts, (), (), proposals=(unsafe,))


@pytest.mark.parametrize(
    "related", ["sessionMedium", "sessionDefaultChannelGroup", "sessionSourceMedium"]
)
def test_source_classification_requires_matching_evidence_and_scope(related):
    context = {
        "facts": [
            {"id": "source_only", "dimension_fields": ["sessionSource"]},
            {"id": "other_report", "dimension_fields": [related]},
            {"id": "same_report", "dimension_fields": ["sessionSource", related]},
            {"id": "wrong_scope", "dimension_fields": ["firstUserSource", related]},
            {
                "id": "filtered",
                "dimension_fields": ["sessionSource"],
                "filters": [{"field_name": related}],
            },
            {
                "id": "first_user",
                "dimension_fields": ["firstUserSource", "firstUserMedium"],
            },
        ]
    }
    result = measurement_context(context)
    assert [e["source_without_medium_or_channel"] for e in result["evidence"]] == [
        True,
        False,
        False,
        True,
        False,
        False,
    ]
    assert result["evidence"][0]["dimension_and_filter_fields"] == ["sessionSource"]


def test_unknown_metric_does_not_block_but_evidence_and_catalog_remain_required():
    item = proposal(evaluation=evaluation()).model_copy(update={"success_metric": None})
    decision = finish().model_copy(update={"proposals": (item,)})
    AgentService.validate_decision(
        decision, SiteContext(), [{"evidence_ids": ["evidence_ok"]}], set()
    )
    with pytest.raises(ValueError, match="evidence_or_metric"):
        AgentService.validate_decision(decision, SiteContext(), [], set())
    invented = decision.model_copy(
        update={
            "proposals": (
                item.model_copy(update={"success_metric": "unregistered_click"}),
            )
        }
    )
    with pytest.raises(ValueError, match="evidence_or_metric"):
        AgentService.validate_decision(
            invented, SiteContext(), [{"evidence_ids": ["evidence_ok"]}], set()
        )


def test_new_generation_requires_reasoning_but_old_saved_proposal_stays_readable():
    old = proposal().model_dump(mode="json", exclude={"evaluation"})
    assert Proposal.model_validate(old).evaluation is None
    decision = finish().model_copy(update={"proposals": (proposal(),)})
    with pytest.raises(ValidationError):
        AdvisoryDecision.model_validate(decision.model_dump(mode="json"))
    new = {
        **old,
        "evaluation": evaluation().model_dump(mode="json"),
        "success_metric": None,
    }
    workspace = AgentWorkspace(
        proposals=(Proposal.model_validate(old), Proposal.model_validate(new))
    )
    record = AgentRecord(
        id="measurement",
        kind="workspace",
        owner_id="qa-user",
        data_source_set_id="qa_synthetic_web",
        revision=1,
        payload=workspace.model_dump(mode="json"),
    )
    stored = _encode_record(record)
    assert_firestore_values(stored)
    restored = AgentWorkspace.model_validate(_decode_record(stored).payload)
    assert restored == workspace


@pytest.mark.asyncio
async def test_measurement_context_propagates_to_selection_and_query():
    director, calls = vertex([selection(), query(metric_terms=["page_views"])])
    supplied = {
        "user_question": "PVを確認",
        "facts": [{"id": "f", "dimension_fields": ["sessionSource"]}],
    }
    await director.decide(supplied)
    for call in calls:
        assert json.loads(call["contents"])[
            "measurement_context"
        ] == measurement_context(supplied)
        assert MEASUREMENT_INSTRUCTIONS in call["config"].system_instruction
    assert MEASUREMENT_INSTRUCTIONS in SUMMARY_INSTRUCTIONS


@pytest.mark.asyncio
async def test_service_passes_current_proposal_reasoning_to_summary_and_storage():
    agent, repository, _ = await build()
    capture = CaptureDirector(seed=True)
    seed_decide = capture.decide

    async def decide(context):
        if capture.seed:
            return await seed_decide(context)
        item = proposal(evaluation=evaluation()).model_copy(
            update={
                "success_metric": None,
                "evidence_ids": tuple(context["facts"][0]["evidence_ids"]),
            }
        )
        return finish().model_copy(update={"proposals": (item,)})

    capture.decide = decide
    agent.director = capture
    agent.orchestrator._executor = PageExecutor()
    events = await turn(agent, "2026年5月のページと参照元別PV", "measurement")
    assert events[-1]["data"]["status"] == "complete"
    assert capture.summary_input["proposals"][0][
        "evaluation"
    ] == evaluation().model_dump(mode="json")
    assert capture.summary_input["proposals"][0]["success_metric"] is None
    assert capture.summary_input["analysis_capabilities"]
    restored = await agent.workspace("qa-user", "as_synthetic_eval")
    assert restored.proposals[0].evaluation == evaluation()
    messages = await repository.list_messages("as_synthetic_eval")
    assert "## 改善仮説の評価方法（未検証）" in messages[-1].content
    assert evaluation().metric_limitations in messages[-1].content
    assert not restored.report_requested
