"""Offline evaluation-harness checks; these do not score real-model quality."""

import json
from pathlib import Path

import pytest
from evaluation.site_context import contexts, evaluate

from analytics_ai.agent.models import SiteContext
from analytics_ai.agent.service import AgentService
from tests.test_agent import finish, proposal

PROFILE = (
    Path(__file__).resolve().parent / "fixtures/synthetic_site_profile.json"
)


def test_draft_is_valid_and_labels_unconfirmed_constraints():
    site = SiteContext.model_validate_json(PROFILE.read_text())
    assert site.site_basics and site.goals
    assert "暫定" in site.technical_constraints
    assert "暫定" in site.content_policy
    assert site.allowed_surfaces == ("home", "listing", "detail", "content")


@pytest.mark.asyncio
async def test_self_test_is_offline_and_visits_all_variants(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("live_model_not_opted_in")

    monkeypatch.setattr("evaluation.site_context.AuditedVertex", forbidden)
    await evaluate(PROFILE, False)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    wiring = [row for row in rows if row["kind"] == "wiring"]
    assert {row["variant"] for row in wiring} == {
        "blank",
        "sapporo",
        "strict_no_changes",
        "sapporo_repeat",
    }
    assert all(row["saved_and_loaded"] and row["fact_count"] == 1 for row in wiring)
    assert rows[-1]["model_calls"] == rows[-1]["persistent_writes"] == 0


@pytest.mark.asyncio
async def test_settings_change_context_but_not_question_or_evidence():
    site = SiteContext.model_validate_json(PROFILE.read_text())
    blank = await contexts(SiteContext())
    configured = await contexts(site)
    fields = {
        "site_basics": "site_basics",
        "goals": "goals",
        "technical_background": "technical_constraints",
        "content_direction": "content_policy",
    }
    for empty, populated in zip(blank, configured, strict=True):
        for field, stored_field in fields.items():
            assert empty["site_context"][field] == ""
            assert populated["site_context"][field] == getattr(site, stored_field)
    assert blank[2]["user_question"] == configured[2]["user_question"]
    assert len(blank[2]["facts"]) == len(configured[2]["facts"]) == 1
    for field in ("category_values", "calculations", "dimension_fields", "metric"):
        assert blank[2]["facts"][0][field] == configured[2]["facts"][0][field]


@pytest.mark.asyncio
async def test_targeted_recheck_does_not_run_unrelated_variants(capsys):
    await evaluate(PROFILE, False, variant="strict_no_changes", selected_phase="plan")
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["variant"] for r in rows if r["kind"] == "wiring"] == [
        "strict_no_changes"
    ]


@pytest.mark.parametrize(
    "preferences,action",
    [
        ({"allowed_surfaces": ()}, "none"),
        ({"allow_new_content": False}, "new"),
        ({"allow_rewrite": False}, "rewrite"),
    ],
)
def test_legacy_preferences_do_not_prohibit_proposals(preferences, action):
    decision = finish().model_copy(
        update={"proposals": (proposal(content_action=action),)}
    )
    AgentService.validate_decision(
        decision,
        SiteContext(**preferences),
        [{"evidence_ids": ["evidence_ok"]}],
        {"active_users"},
    )
