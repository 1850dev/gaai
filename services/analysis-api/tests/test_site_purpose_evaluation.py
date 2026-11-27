"""Controlled site-purpose evaluation wiring, not model semantic scores."""

import json

import pytest
from evaluation.site_context import (
    contexts,
    evaluate,
    non_site_fingerprint,
    purpose_variants,
    with_site_context,
)

from analytics_ai.agent.models import SiteContext
from tests.test_site_context_evaluation import PROFILE


def test_counterfactual_changes_only_goal_not_permissions_or_site_content():
    site = SiteContext.model_validate_json(PROFILE.read_text())
    variants = dict(purpose_variants(site))
    assert set(variants) == {"blank", "sapporo", "discovery"}
    assert variants["sapporo"].model_dump(exclude={"goals"}) == variants[
        "discovery"
    ].model_dump(exclude={"goals"})
    assert variants["sapporo"].goals != variants["discovery"].goals
    assert "仮想目的" in variants["discovery"].goals


@pytest.mark.asyncio
async def test_projection_preserves_all_non_site_inputs_without_mutating_base():
    base = await contexts(SiteContext())
    site = SiteContext.model_validate_json(PROFILE.read_text())
    for _, variant in purpose_variants(site):
        projected = with_site_context(base, variant)
        assert [non_site_fingerprint(c) for c in projected] == [
            non_site_fingerprint(c) for c in base
        ]
        assert projected[2]["facts"] == base[2]["facts"]
        projected[2]["proposals"].append({"test": True})
        assert not base[2]["proposals"]


@pytest.mark.asyncio
async def test_comparison_is_offline_by_default_and_repeats_in_reverse_order(
    monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("model_not_opted_in")

    monkeypatch.setattr("evaluation.site_context.AuditedVertex", forbidden)
    await evaluate(PROFILE, False, suite="purposes", repetitions=2)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    runs = [r for r in rows if r["kind"] == "wiring"]
    assert [(r["repetition"], r["variant"]) for r in runs] == [
        (1, "blank"),
        (1, "sapporo"),
        (1, "discovery"),
        (2, "discovery"),
        (2, "sapporo"),
        (2, "blank"),
    ]
    assert all(
        r["non_site_fingerprints"] == runs[0]["non_site_fingerprints"] for r in runs
    )
    assert rows[-1]["model_calls"] == rows[-1]["persistent_writes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suite,repeats", [("unknown", 1), ("purposes", 3), ("purposes", 0)]
)
async def test_invalid_or_unbounded_suite_is_rejected(suite, repeats):
    with pytest.raises(ValueError, match="invalid_evaluation"):
        await evaluate(PROFILE, False, suite=suite, repetitions=repeats)
