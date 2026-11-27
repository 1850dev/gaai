"""Goal/evidence paths survive turns without becoming measured facts or policy."""

import json

import pytest
from evaluation.live_continuation import build, turn
from evaluation.site_context import CaptureDirector, PageExecutor
from pydantic import ValidationError

from analytics_ai.agent.director import (
    DIRECTOR_INSTRUCTIONS,
    SELECTION_INSTRUCTIONS,
    SUMMARY_INSTRUCTIONS,
    AdvisoryDecision,
    AdvisoryGoalCheck,
    ga4_argument_context,
)
from analytics_ai.agent.models import (
    AgentRecord,
    AgentWorkspace,
    ProposalEvaluation,
    SiteContext,
)
from analytics_ai.agent.presentation import render_summary
from analytics_ai.agent.purpose import (
    PURPOSE_INSTRUCTIONS,
    goal_sources,
    prior_goal_context,
)
from analytics_ai.agent.store import _decode_record, _encode_record
from analytics_ai.authorization.service import AccessDenied
from tests.test_advisory_metric_contract import answer
from tests.test_agent import finish, proposal
from tests.test_agent_store import assert_firestore_values
from tests.test_measurement_reasoning import evaluation


def proposed():
    return proposal(evaluation=evaluation()).model_copy(update={"success_metric": None})


def test_goal_sources_excludes_model_hypotheses_and_pending_agreements():
    sources = goal_sources(
        {
            "user_question": "今は再訪を評価したい",
            "site_context": {"goals": "案内を探しやすくする"},
            "working_memory": {
                "entries": [
                    {
                        "kind": "objective",
                        "origin": "user_statement",
                        "text": "魅力を発見してほしい",
                    },
                    {
                        "kind": "hypothesis",
                        "origin": "agent_hypothesis",
                        "text": "広告で売上を増やす",
                    },
                ]
            },
            "confirmed_agreements": [
                {"status": "pending", "text": "未確定"},
                {"status": "confirmed", "text": "案内を充実させたい"},
            ],
        }
    )
    assert sources == {
        "user_request": ["今は再訪を評価したい"],
        "site_context": ["案内を探しやすくする"],
        "user_memory": ["魅力を発見してほしい", "案内を充実させたい"],
    }


@pytest.mark.parametrize("basis", ["user_request", "site_context", "user_memory"])
def test_goal_quote_is_bound_to_the_correct_source_not_a_global_catalog(basis):
    data = evaluation().goal_checks[0].model_dump()
    data.update(goal_basis=basis, goal_quote="再訪", evidence_status="unmeasured")
    sources = {basis: ["魅力発見と再訪を育てたい"]}
    assert AdvisoryGoalCheck.model_validate(data, context={"goal_sources": sources})
    with pytest.raises(ValidationError, match="current_source_verbatim"):
        AdvisoryGoalCheck.model_validate(
            data, context={"goal_sources": {basis: ["案内の効率化"]}}
        )


@pytest.mark.parametrize("state", ["proxy_only", "unmeasured"])
def test_unconfirmed_goal_cannot_be_silently_relabelled_as_an_agreed_goal(state):
    data = evaluation().goal_checks[0].model_dump()
    data["evidence_status"] = state
    with pytest.raises(ValidationError, match="unconfirmed_goal"):
        AdvisoryGoalCheck.model_validate(data)


@pytest.mark.parametrize("missing", [None, []])
def test_new_advice_requires_goal_evidence_path_but_old_evaluations_load(missing):
    payload = answer(None)
    values = payload["proposals"][0]["evaluation"]
    if missing is None:
        del values["goal_checks"]
    else:
        values["goal_checks"] = missing
    assert ProposalEvaluation.model_validate(values).goal_checks == ()
    with pytest.raises(ValidationError) as error:
        AdvisoryDecision.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (
        "proposals",
        0,
        "evaluation",
        "goal_checks",
    )


@pytest.mark.parametrize(
    "bad", ["goal_achieved", "confirmed_outcome", "measured_success"]
)
def test_proposed_evaluation_cannot_encode_goal_attainment(bad):
    values = evaluation().model_dump(mode="json")
    values["goal_checks"][0]["evidence_status"] = bad
    with pytest.raises(ValidationError):
        ProposalEvaluation.model_validate(values)


def test_goal_path_is_saved_as_proposal_not_agreement_or_finding():
    workspace = AgentWorkspace(proposals=(proposed(),), proposals_site_revision=3)
    record = AgentRecord(
        id="purpose",
        kind="workspace",
        owner_id="u",
        data_source_set_id="d",
        revision=1,
        payload=workspace.model_dump(mode="json"),
    )
    encoded = _encode_record(record)
    assert_firestore_values(encoded)
    restored = AgentWorkspace.model_validate(_decode_record(encoded).payload)
    assert restored == workspace
    assert not restored.memory.entries and not restored.sections


@pytest.mark.parametrize("revision", [None, 2, 3])
def test_prior_context_marks_revision_and_missing_evidence_without_changing_goals(
    revision,
):
    workspace = AgentWorkspace(
        proposals=(proposed(),), proposals_site_revision=revision
    )
    site = SiteContext(revision=3, goals="再訪を検討する")
    context = prior_goal_context(workspace, site, [])
    assert context["site_revision_changed"] is (revision != 3)
    assert context["items"][0]["support_available_this_turn"] is False
    assert context["origin"] == "prior_unverified_proposals_not_current_findings"
    assert "再訪" not in json.dumps(context, ensure_ascii=False)
    assert site.goals == "再訪を検討する"
    known = prior_goal_context(workspace, site, [{"evidence_ids": ["evidence_ok"]}])
    assert known["items"][0]["support_available_this_turn"] is True


def test_empty_legacy_context_does_not_invent_goals_or_queries():
    workspace = AgentWorkspace(proposals=(proposal(),))
    assert prior_goal_context(workspace, SiteContext(), [])["items"] == []
    projected = ga4_argument_context({"prior_goal_context": {"items": ["unverified"]}})
    assert projected["prior_goal_context"] == {"items": ["unverified"]}
    assert "plan" not in projected


@pytest.mark.parametrize(
    "instructions",
    [DIRECTOR_INSTRUCTIONS, SELECTION_INSTRUCTIONS, SUMMARY_INSTRUCTIONS],
)
def test_purpose_guidance_reaches_all_stages_without_site_bans(instructions):
    assert PURPOSE_INSTRUCTIONS in instructions
    assert "Current explicit user intent overrides" in instructions
    assert "do not manufacture differences" in instructions


def test_goal_path_survives_summary_failure_in_saved_markdown():
    facts = [
        {
            "id": "f",
            "metric": "page_views",
            "text": "PV: 10",
            "evidence_ids": ["evidence_ok"],
        }
    ]
    body = render_summary(None, facts, (), (), proposals=(proposed(),))
    check = evaluation().goal_checks[0]
    for value in (check.goal, check.required_evidence, check.gap, check.next_check):
        assert value in body
    assert "事業目的の確認が必要" in body


@pytest.mark.asyncio
async def test_followup_retains_goal_checks_across_trimmed_history_and_site_change():
    agent, repository, _ = await build()
    capture = CaptureDirector(seed=True)
    seed_decide = capture.decide
    agent.director = capture

    class CountedPages(PageExecutor):
        calls = 0

        async def execute(self, invocation):
            self.calls += 1
            return await super().execute(invocation)

    executor = CountedPages()
    agent.orchestrator._executor = executor

    async def create(context):
        if capture.seed:
            return await seed_decide(context)
        item = proposed().model_copy(
            update={"evidence_ids": tuple(context["facts"][0]["evidence_ids"])}
        )
        return finish().model_copy(update={"proposals": (item,)})

    capture.decide = create
    site = await agent.save_site(
        "qa-user", "qa_synthetic_web", SiteContext(goals="案内の発見を支援")
    )
    await turn(agent, "2026年5月のページと参照元別PV", "initial-purpose")
    saved = await agent.workspace("qa-user", "as_synthetic_eval")
    assert saved.proposals_site_revision == site.revision
    session = await agent.session("qa-user", "as_synthetic_eval")
    await agent.checkpoint(
        session,
        saved,
        turns=tuple({"role": "user", "content": "別の話題"} for _ in range(20)),
    )
    updated = await agent.save_site(
        "qa-user",
        "qa_synthetic_web",
        site.model_copy(update={"goals": "催事の魅力発見と再訪を確認"}),
    )

    async def discuss(context):
        capture.context = context
        return finish()

    capture.decide = discuss
    await turn(
        agent, "前の案は、今は再訪という目的から検討してください", "revisit-purpose"
    )
    assert capture.context["workspace"]["history_truncated"] is True
    assert "今は再訪" in capture.context["user_question"]
    await turn(
        agent,
        "新しいデータ取得はせず、前の案の未確認事項を説明して",
        "followup-purpose",
    )
    for context in (capture.context, capture.summary_input):
        assert context["site_context"]["goals"] == updated.goals
        previous = context["prior_goal_context"]
        assert previous["site_revision_changed"] is True
        assert previous["items"][0]["goal_checks"] == [
            g.model_dump(mode="json") for g in evaluation().goal_checks
        ]
    assert capture.summary_input["data_acquisition_allowed"] is False
    restored = await agent.workspace("qa-user", session.id)
    assert restored.proposals_site_revision == site.revision  # not relabelled as new
    assert restored.proposals == saved.proposals
    assert not restored.report_requested
    assert executor.calls == 1  # seed only; both discussion turns reuse it
    messages = await repository.list_messages(session.id)
    assert any("評価したい目的" in m.content for m in messages)
    with pytest.raises(AccessDenied):
        await agent.workspace("other-user", session.id)
