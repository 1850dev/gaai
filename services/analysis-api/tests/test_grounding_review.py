"""SDK-level bounded review wiring, not a simulation of model semantic accuracy."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from analytics_ai.agent.director import PlannerPhaseTimeout, PlannerPhaseValidationError
from analytics_ai.agent.grounding import (
    GroundingRejected,
    GroundingReview,
    purpose_passages,
    review_context,
)
from analytics_ai.agent.models import AgentSummary
from tests.test_advisory_metric_contract import answer, chosen, supplied
from tests.test_director_continuation import vertex


def issue(code="source_identity_inference", path="proposals.0.title"):
    return {**review_pass(), "issues": [{"code": code, "path": path}]}


def review_pass():
    return {
        "source_reasoning": "Observed source labels only.",
        "measurement_reasoning": "No click or path claim.",
        "capability_reasoning": "No unsupported next action.",
        "purpose_reasoning": "Goal and missing evidence are distinguished.",
        "purpose_coverage": [],
        "issues": [],
    }


def missing_purpose(quote):
    return issue("purpose_drift", "explanation") | {
        "purpose_coverage": [
            {
                "goal_quote": quote,
                "disposition": "missing",
                "explanation": "必要な評価観点が省略されています。",
                "candidate_quote": None,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["advice", "summary"])
async def test_missing_goal_repair_uses_private_verified_source_not_diagnostics(phase):
    quote = "PRIVATE_GOAL_再訪を育てる"
    context = supplied() | {"site_context": {"goals": quote}}
    bad_review = missing_purpose(quote)
    responses = (
        [chosen(), answer(), bad_review, answer(), review_pass()]
        if phase == "advice"
        else [summary(), bad_review, summary(), review_pass()]
    )
    director, calls = vertex(responses, grounding=True)
    await (
        director.decide(context) if phase == "advice" else director.summarize(context)
    )
    repaired = json.loads(calls[3 if phase == "advice" else 2]["contents"])
    assert repaired["missing_purpose_quotes"] == [quote]
    assert quote not in repaired["validation_error"]
    error = GroundingRejected(GroundingReview.model_validate(bad_review), context)
    assert quote not in str(error) and quote not in error.feedback
    assert error.purpose_repair == [quote]


def test_reviewer_invented_goal_is_not_used_as_repair_instruction():
    review = GroundingReview.model_validate(missing_purpose("架空の事業目的"))
    error = GroundingRejected(review, {"site_context": {"goals": "再訪を育てる"}})
    assert error.purpose_repair == []


def summary():
    return {
        "explanation": "取得した参照元名の内訳です。",
        "assessments": [{"interpretation": "改善仮説です。", "fact_ids": ["f"]}],
    }


def test_missing_goal_requires_explicit_quality_issue():
    candidate = review_pass() | {
        "purpose_coverage": [
            {
                "goal_quote": "再訪を育てる",
                "disposition": "missing",
                "explanation": "改善案が再訪の確認方法を示していない。",
                "candidate_quote": None,
            }
        ],
    }
    with pytest.raises(ValidationError, match="missing_goal_requires_purpose_issue"):
        GroundingReview.model_validate(candidate)
    candidate["issues"] = [{"code": "purpose_drift", "path": "proposals"}]
    assert GroundingReview.model_validate(candidate).issues


@pytest.mark.parametrize(
    "status", ["addressed", "unmeasured_with_next_check", "not_relevant_to_request"]
)
def test_goal_coverage_allows_explicit_limits_and_narrow_tasks(status):
    candidate = review_pass() | {
        "purpose_coverage": [
            {
                "goal_quote": "再訪を育てる",
                "disposition": status,
                "explanation": "現在の依頼と取得済み根拠との関係を確認した。",
                "candidate_quote": "再訪の定義を担当者に確認",
            }
        ],
    }
    assert not GroundingReview.model_validate(
        candidate, context={"purpose_passages": ["再訪の定義を担当者に確認"]}
    ).issues


def test_goal_echo_is_not_candidate_evaluation_evidence():
    from analytics_ai.agent.director import AdvisoryDecision
    from tests.test_advisory_metric_contract import answer

    payload = answer(None)
    candidate = AdvisoryDecision.model_validate(payload)
    passages = purpose_passages(candidate)
    check = candidate.proposals[0].evaluation.goal_checks[0]
    assert check.next_check in passages
    assert check.goal not in passages
    data = review_pass() | {
        "purpose_coverage": [
            {
                "goal_quote": "再訪を育てる",
                "disposition": "addressed",
                "explanation": "誤った合格例",
                "candidate_quote": "回答にはない再訪の説明",
            }
        ]
    }
    with pytest.raises(ValidationError, match="candidate_evaluation_quote"):
        GroundingReview.model_validate(data, context={"purpose_passages": passages})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,path",
    [
        ("source_identity_inference", "proposals.0.title"),
        ("source_channel_inference", "proposals.0.hypothesis"),
        ("metric_meaning_mismatch", "proposals.0.evaluation.metric_reason"),
        ("aggregate_as_path_or_click", "proposals.0.validation_plan"),
        ("measurement_prerequisite_missing", "proposals.0.evaluation.prerequisites"),
        ("unsupported_analysis_offer", "questions.0"),
        ("no_fetch_instruction_ignored", "questions.0"),
    ],
)
async def test_advice_review_repairs_all_fields_once_without_reselection(code, path):
    before, after = answer(), answer()
    before["proposals"][0]["title"] = "アプリ会員向けの改善"
    after["proposals"][0]["title"] = "参照元名ごとの改善仮説"
    director, calls = vertex(
        [chosen(), before, issue(code, path), after, review_pass()], grounding=True
    )
    result = await director.decide({**supplied(), "data_acquisition_allowed": False})
    assert result.proposals[0].title == after["proposals"][0]["title"]
    assert len(calls) == 5  # select + (generate + review) twice, no GA4 tools
    repair = json.loads(calls[3]["contents"])["validation_error"]
    assert code in repair and path in repair and "アプリ" not in repair
    for index in (2, 4):
        payload = json.loads(calls[index]["contents"])
        assert payload["data_acquisition_allowed"] is False
        assert payload["facts"] == supplied()["facts"]
        assert calls[index]["config"].automatic_function_calling.disable


@pytest.mark.asyncio
async def test_advice_twice_rejected_is_never_returned():
    director, calls = vertex(
        [chosen(), answer(), issue(), answer(), issue()], grounding=True
    )
    with pytest.raises(PlannerPhaseValidationError) as error:
        await director.decide(supplied())
    assert "source_identity_inference" in error.value.feedback
    assert error.value.error_code == "agent_advice_grounding_failed"
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_summary_questions_are_reviewed_and_repaired():
    bad, good = summary(), summary()
    bad["questions"] = ["次に閲覧されたページの分析を進めますか？"]
    good["questions"] = ["クリック計測の定義を確認できますか？"]
    director, calls = vertex(
        [bad, issue("unsupported_analysis_offer", "questions.0"), good, review_pass()],
        grounding=True,
    )
    result = await director.summarize(supplied())
    assert result.questions == tuple(good["questions"])
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_summary_twice_rejected_raises_for_existing_fact_only_fallback():
    director, calls = vertex([summary(), issue(), summary(), issue()], grounding=True)
    with pytest.raises(GroundingRejected):
        await director.summarize(supplied())
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [{}, {"issues": [{"code": "ok", "path": "x"}]}])
async def test_invalid_reviewer_response_is_not_a_pass(malformed):
    director, _ = vertex(
        [chosen(), answer(), malformed, answer(), malformed], grounding=True
    )
    with pytest.raises(PlannerPhaseValidationError):
        await director.decide(supplied())


@pytest.mark.asyncio
async def test_review_shares_advice_deadline(monkeypatch):
    director, _ = vertex([chosen(), answer()], grounding=True)
    original = director._review_grounding

    async def slow(*args):
        await asyncio.sleep(1)
        await original(*args)

    monkeypatch.setattr(director, "_review_grounding", slow)
    monkeypatch.setattr(director, "phase_timeouts", {"selection": 1, "advice": 0.02})
    with pytest.raises(PlannerPhaseTimeout) as error:
        await director.decide(supplied())
    assert error.value.phase == "advice"


def test_review_does_not_merge_evidence_scope_or_copy_history():
    context = {
        **supplied(),
        "site_context": {"role": "background_not_policy"},
        "analysis_capabilities": {"methods": {"funnel": {"registered_funnels": []}}},
        "historical_analyses": ["excluded"],
        "facts": [
            {"id": "f", "dimension_fields": ["sessionSource"]},
            {"id": "other", "dimension_fields": ["sessionMedium"]},
        ],
    }
    projected = review_context(context, AgentSummary.model_validate(summary()))
    assert projected["facts"] == context["facts"]
    assert "historical_analyses" not in projected
    assert projected["analysis_capabilities"] == context["analysis_capabilities"]
    assert projected["measurement_context"]["evidence"][0][
        "source_without_medium_or_channel"
    ]


def test_feedback_has_only_validated_codes_and_structural_paths():
    assert GroundingReview.model_validate(issue(path="proposals[0].title"))
    with pytest.raises(ValidationError):
        GroundingReview.model_validate(issue(path="private/source-name"))
    with pytest.raises(ValidationError):
        GroundingReview.model_validate({"issues": [], "new_query": "invented"})


@pytest.mark.asyncio
async def test_failed_summary_review_keeps_facts_and_charts_without_refetch(repository):
    from types import SimpleNamespace

    from tests.test_agent_continuation import run_turn
    from tests.test_director_continuation import query, selection
    from tests.test_time_grain_intent import harness

    agent, _, executor, orchestrator = await harness(repository, [])
    director, _ = vertex([], grounding=True)
    agent.director = director
    responses = [
        selection(),
        query(
            metric_terms=["active_users"],
            dimension_terms=["date"],
            time_grain="day",
            period={"start": "2026-05-01", "end": "2026-05-31"},
        ),
        selection("evidence.answer"),
    ]
    reviews = []

    async def generate(**kwargs):
        if responses:
            return SimpleNamespace(text=json.dumps(responses.pop(0)))
        if "issues" in kwargs["config"].response_json_schema["properties"]:
            reviews.append(True)
            return SimpleNamespace(
                text=json.dumps(issue("unsupported_analysis_offer", "questions.0"))
            )
        facts = json.loads(kwargs["contents"])["facts"]
        return SimpleNamespace(
            text=json.dumps(
                {
                    "explanation": "取得済み推移です。",
                    "assessments": [
                        {
                            "interpretation": "推移を確認できます。",
                            "fact_ids": [facts[0]["id"]],
                        }
                    ],
                    "questions": ["次に閲覧したページの遷移分析を進めますか？"],
                }
            )
        )

    director.client.aio.models.generate_content = generate
    await run_turn(agent, "2026年5月のUUを日別に。比較不要です。", "review-fallback")
    assert len(executor.invocations) == 1 and len(reviews) == 2
    session = await agent.session("user-a", "as_orchestrator")
    messages = await repository.list_messages(session.id)
    assert messages[-1].finding_ids and messages[-1].evidence_ids
    assert "次に閲覧したページの遷移分析" not in messages[-1].content
    assert "その文章は採用せず、取得済みの根拠を表示" in messages[-1].content
    charts = await orchestrator.restore_results(session=session, messages=messages)
    assert len(charts) == 1
