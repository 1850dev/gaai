"""Regression coverage for matching category comparisons across periods."""

from decimal import Decimal

import pytest

from analytics_ai.domain.models import (
    CalculationKind,
    SemanticDefinition,
    SemanticScope,
)
from analytics_ai.orchestrator.skills import AnalysisSkillId
from analytics_ai.repositories.memory import InMemoryRepository
from tests.test_orchestrator import active_users_plan, add_session, build_orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous,expected_value,expected_rate",
    [
        (
            [
                {"channel": "Paid Search", "activeUsers": "1000"},
                {"channel": "Organic Search", "activeUsers": "80"},
            ],
            "80",
            "0.5",
        ),
        ([{"channel": "Paid Search", "activeUsers": "1000"}], None, None),
        ([{"channel": "Organic Search", "activeUsers": "0"}], "0", None),
        (
            [
                {"channel": "Organic Search", "activeUsers": "80"},
                {"channel": "Organic Search", "activeUsers": "60"},
            ],
            None,
            None,
        ),
    ],
    ids=["leader-changed", "category-missing", "explicit-zero", "ambiguous-category"],
)
async def test_category_comparison_uses_matching_category_only(
    repository: InMemoryRepository,
    previous: list[dict[str, str]],
    expected_value: str | None,
    expected_rate: str | None,
) -> None:
    await add_session(repository)
    definition = SemanticDefinition(
        id="global-channel",
        canonical_name="channel",
        aliases=("channel",),
        kind="dimension",
        scope=SemanticScope.GLOBAL,
        ga4_mapping={"dimension": "sessionDefaultChannelGroup"},
        version="a-web-v1",
    )
    repository.semantic_definitions[definition.id] = definition
    plan = active_users_plan().model_copy(
        update={
            "skill_id": AnalysisSkillId.BREAKDOWN_RANK,
            "dimension_terms": ("channel",),
        }
    )
    rows = [
        {
            "sessionDefaultChannelGroup": "Organic Search",
            "activeUsers": "120",
            "dateRange": "current",
        },
        {
            "sessionDefaultChannelGroup": "Paid Search",
            "activeUsers": "110",
            "dateRange": "current",
        },
        *(
            {
                "sessionDefaultChannelGroup": row["channel"],
                "activeUsers": row["activeUsers"],
                "dateRange": "comparison",
            }
            for row in previous
        ),
    ]
    orchestrator, _ = build_orchestrator(repository, [plan], {"rows": rows})
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="チャネル別のユーザー数を比較して",
        request_id="category_comparison",
    )
    finding = result.findings[0]
    assert finding.value == "120"
    assert finding.comparison_value == expected_value
    assert finding.change_rate == expected_rate
    assert "Organic Search" in finding.text
    assert "Paid Search" not in finding.text
    if expected_value is None:
        assert all(
            fact.kind
            not in {
                CalculationKind.DIFFERENCE,
                CalculationKind.RATIO,
                CalculationKind.CHANGE_RATE,
            }
            for fact in finding.calculations
        )
    elif expected_rate is not None:
        difference = next(
            fact
            for fact in finding.calculations
            if fact.kind == CalculationKind.DIFFERENCE
        )
        assert Decimal(difference.value) == Decimal("40")
    assert finding.evidence_ids == (result.evidence[0].id,)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_matching_tuple", [True, False])
async def test_category_comparison_matches_every_dimension(
    repository: InMemoryRepository, include_matching_tuple: bool
) -> None:
    await add_session(repository)
    for canonical_name, field in (
        ("channel", "sessionDefaultChannelGroup"),
        ("device_category", "deviceCategory"),
    ):
        definition = SemanticDefinition(
            id=f"global-{canonical_name}",
            canonical_name=canonical_name,
            aliases=(canonical_name,),
            kind="dimension",
            scope=SemanticScope.GLOBAL,
            ga4_mapping={"dimension": field},
            version="a-web-v1",
        )
        repository.semantic_definitions[definition.id] = definition
    plan = active_users_plan().model_copy(
        update={
            "skill_id": AnalysisSkillId.BREAKDOWN_RANK,
            "dimension_terms": ("channel", "device_category"),
        }
    )
    rows = [
        {
            "sessionDefaultChannelGroup": "Organic Search",
            "deviceCategory": "mobile",
            "activeUsers": "120",
            "dateRange": "current",
        },
        {
            "sessionDefaultChannelGroup": "Organic Search",
            "deviceCategory": "desktop",
            "activeUsers": "1000",
            "dateRange": "comparison",
        },
    ]
    if include_matching_tuple:
        rows.append(
            {
                "sessionDefaultChannelGroup": "Organic Search",
                "deviceCategory": "mobile",
                "activeUsers": "80",
                "dateRange": "comparison",
            }
        )
    orchestrator, _ = build_orchestrator(repository, [plan], {"rows": rows})
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="チャネルとデバイス別のユーザー数を比較して",
        request_id="category_tuple_comparison",
    )
    finding = result.findings[0]
    assert finding.comparison_value == ("80" if include_matching_tuple else None)
    assert finding.change_rate == ("0.5" if include_matching_tuple else None)
    matched = finding.category_comparisons[0]
    assert matched.dimensions == {
        "sessionDefaultChannelGroup": "Organic Search",
        "deviceCategory": "mobile",
    }
    assert matched.comparison_value == finding.comparison_value
    assert matched.difference == ("40" if include_matching_tuple else None)


def test_bounded_comparison_keeps_missing_zero_and_ambiguous_distinct() -> None:
    from analytics_ai.orchestrator.category_comparison import compare_categories

    current = [
        {"country": "Japan", "activeUsers": "120"},
        {"country": "Unknown", "activeUsers": "10"},
        {"country": "Zero", "activeUsers": "5"},
        {"country": "Duplicate", "activeUsers": "20"},
        {"country": "Duplicate", "activeUsers": "30"},
    ]
    before = [
        {"country": "Japan", "activeUsers": "80"},
        {"country": "Zero", "activeUsers": "0"},
        {"country": "Duplicate", "activeUsers": "15"},
        {"country": "MissingNow", "activeUsers": "70"},
    ]
    results = {
        r.dimensions["country"]: r
        for r in compare_categories(
            current, before, fields=("country",), metric="activeUsers"
        )
    }
    assert results["Japan"].difference == "40"
    assert results["Japan"].change_rate == "0.5"
    assert results["Unknown"].status == "current_only"
    assert results["Unknown"].difference is None
    assert results["Zero"].difference == "5"
    assert results["Zero"].change_rate is None
    assert results["Duplicate"].status == "ambiguous"
    assert results["Duplicate"].difference is None
    assert results["MissingNow"].current_value is None
    assert results["MissingNow"].status == "comparison_only"


def test_short_answer_selection_keeps_baseline_only_category() -> None:
    from analytics_ai.orchestrator.category_comparison import compare_categories

    current = [
        {"eventName": f"event-{i}", "eventCount": str(100 - i)} for i in range(10)
    ]
    baseline = [*current, {"eventName": "baseline-only", "eventCount": "1000"}]
    comparisons = compare_categories(
        current, baseline, fields=("eventName",), metric="eventCount"
    )
    assert len(comparisons) == 11
    assert any(c.dimensions["eventName"] == "baseline-only" for c in comparisons[:10])


@pytest.mark.asyncio
async def test_comparison_facts_survive_storage_and_summary_failure(
    repository: InMemoryRepository,
) -> None:
    from analytics_ai.agent.facts import finding_context
    from analytics_ai.agent.presentation import render_summary
    from analytics_ai.domain.models import AnalysisFinding

    await add_session(repository)
    definition = SemanticDefinition(
        id="source",
        canonical_name="session_source",
        aliases=("参照元",),
        kind="dimension",
        scope=SemanticScope.GLOBAL,
        version="a-web-v1",
        ga4_mapping={"dimension": "sessionSource"},
    )
    repository.semantic_definitions[definition.id] = definition
    plan = active_users_plan().model_copy(
        update={"dimension_terms": ("session_source",)}
    )
    rows = [
        {"sessionSource": name, "activeUsers": value, "dateRange": period}
        for name, now, before in (("google", "120", "80"), ("bing", "60", "90"))
        for period, value in (("current", now), ("comparison", before))
    ]
    orchestrator, _ = build_orchestrator(repository, [plan], {"rows": rows})
    result = await orchestrator.analyze(
        user_id="user-a",
        session_id="as_orchestrator",
        content="参照元を比較",
        request_id="compare-persisted",
    )
    restored = AnalysisFinding.model_validate_json(result.findings[0].model_dump_json())
    context = finding_context(restored, list(restored.evidence_ids))
    assert len(context["category_comparisons"]) == 2
    text = render_summary(None, [context], (), ())
    assert "| google | 80 | 120 | 40 | 50.00% |" in text
    assert "| bing | 90 | 60 | -30 | -33.33% |" in text
    assert restored.evidence_ids[0] in text
