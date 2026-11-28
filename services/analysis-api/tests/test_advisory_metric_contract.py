"""Per-call canonical proposal metrics, bounded repair and unchanged evidence guard."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from analytics_ai.agent.director import (
    AdvisoryDecision,
    PlannerPhaseValidationError,
    advisory_schema,
    proposal_metric_terms,
)
from analytics_ai.agent.models import Proposal, SiteContext
from analytics_ai.agent.service import AgentService
from tests.test_agent import finish, proposal
from tests.test_director_continuation import selection, vertex
from tests.test_measurement_reasoning import evaluation


def chosen():
    return {
        **selection("evidence.answer"),
        "workflow_id": "advisory.site_improvement.v1",
    }


def answer(metric="page_views"):
    item = proposal(evaluation=evaluation()).model_copy(
        update={"success_metric": metric}
    )
    return finish().model_copy(update={"proposals": (item,)}).model_dump(mode="json")


def supplied():
    return {
        "user_question": "取得済みの結果から改善案を考えて",
        "catalog": [
            {
                "term": "page_views",
                "kind": "metric",
                "ga4_mapping": {"metric": "screenPageViews"},
                "aliases": ["PV"],
            }
        ],
        "facts": [{"id": "f", "evidence_ids": ["evidence_ok"]}],
    }


def test_only_current_enabled_available_canonical_metrics_enter_schema():
    context = supplied()
    context["catalog"] += [
        {"term": "page_path", "kind": "dimension"},
        {"term": "disabled", "kind": "metric", "enabled": False},
        {"term": "unavailable", "kind": "metric", "available": False},
        {"term": "", "kind": "metric"},
    ]
    terms = proposal_metric_terms(context)
    assert terms == ("page_views",)
    model = advisory_schema(terms)["$defs"]["AdvisoryProposal"]
    assert model["properties"]["success_metric"] == {
        "anyOf": [{"type": "string", "enum": ["page_views"]}, {"type": "null"}]
    }
    assert "success_metric" in model["required"]
    assert advisory_schema(())["$defs"]["AdvisoryProposal"]["properties"][
        "success_metric"
    ] == {"type": "null"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["screenPageViews", "PV", "unknown", "page_path", "other_company_metric"]
)
async def test_bad_metric_repairs_inside_advice_without_reselecting_or_fetching(
    invalid,
):
    director, calls = vertex([chosen(), answer(invalid), answer()])
    result = await director.decide(supplied())
    assert result.proposals[0].success_metric == "page_views"
    assert len(calls) == 3
    repair = json.loads(calls[-1]["contents"])
    assert repair["proposal_metric_terms"] == ["page_views"]
    assert "success_metric" in repair["validation_error"]
    assert invalid not in repair["validation_error"]
    assert (
        calls[1]["config"].response_json_schema
        == calls[2]["config"].response_json_schema
    )
    assert result.action == "finish" and result.plan is None


@pytest.mark.asyncio
async def test_bad_metric_twice_stops_at_existing_phase_budget():
    director, calls = vertex([chosen(), answer("unknown"), answer("unknown")])
    with pytest.raises(PlannerPhaseValidationError) as error:
        await director.decide(supplied())
    assert error.value.phase == "advice"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_empty_catalog_allows_unconfirmed_metric_without_inventing_one():
    director, calls = vertex([chosen(), answer(None)])
    result = await director.decide({**supplied(), "catalog": []})
    assert result.proposals[0].success_metric is None
    assert len(calls) == 2
    AgentService.validate_decision(result, SiteContext(), supplied()["facts"], set())


@pytest.mark.asyncio
async def test_registry_changes_between_calls_do_not_share_metric_choices():
    director, calls = vertex([chosen(), answer(), chosen(), answer(), answer(None)])
    assert (await director.decide(supplied())).proposals[
        0
    ].success_metric == "page_views"
    result = await director.decide(
        {**supplied(), "catalog": [{"term": "sessions", "kind": "metric"}]}
    )
    assert result.proposals[0].success_metric is None
    assert json.loads(calls[3]["contents"])["proposal_metric_terms"] == ["sessions"]


def test_ambiguous_physical_mapping_is_not_silently_normalized():
    context = supplied()
    context["catalog"].append(
        {
            "term": "other_business_pv",
            "kind": "metric",
            "ga4_mapping": {"metric": "screenPageViews"},
        }
    )
    with pytest.raises(ValidationError):
        AdvisoryDecision.model_validate(
            answer("screenPageViews"),
            context={"proposal_metric_terms": proposal_metric_terms(context)},
        )
    assert (
        Proposal.model_validate(answer()["proposals"][0]).success_metric == "page_views"
    )


@pytest.mark.asyncio
async def test_canonical_metric_does_not_bypass_evidence_guard():
    bad = answer()
    bad["proposals"][0]["evidence_ids"] = ["invented_evidence"]
    director, _ = vertex([chosen(), bad])
    result = await director.decide(supplied())
    with pytest.raises(ValueError, match="evidence_or_metric"):
        AgentService.validate_decision(
            result, SiteContext(), supplied()["facts"], {"page_views"}
        )


def test_recorded_q3c3_failure_is_rejected_locally_with_actionable_field_path():
    path = Path(__file__).resolve().parent / "fixtures/q3c3_rejected_decision.json"
    rejected = json.loads(path.read_text())
    with pytest.raises(ValidationError) as error:
        AdvisoryDecision.model_validate(
            rejected, context={"proposal_metric_terms": ("page_views",)}
        )
    assert [e["loc"] for e in error.value.errors()] == [
        ("proposals", 0, "success_metric"),
        ("proposals", 0, "evaluation", "goal_checks"),
        ("proposals", 1, "success_metric"),
        ("proposals", 1, "evaluation", "goal_checks"),
    ]
