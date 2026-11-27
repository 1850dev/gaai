from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from analytics_ai.domain.models import (
    AnalysisFinding,
    AnalysisState,
    DatePeriod,
)
from analytics_ai.orchestrator.answering import AnswerDraft, AnswerRenderer


def finding() -> AnalysisFinding:
    return AnalysisFinding(
        id="finding_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        session_id="as_answer",
        user_id="user-a",
        company_id="company_a",
        data_source_set_id="company_a_web",
        dataset_id="dataset_answer",
        query_hash="a" * 64,
        evidence_ids=("evidence_answer",),
        semantic_version="a-web-v1",
        text="active_users: 120（前月比 20%）",
        metric_field="activeUsers",
        metric_label="active_users",
        value="120",
        comparison_value="100",
        change_rate="0.2",
    )


def test_answer_renderer_inserts_trusted_numeric_finding_verbatim() -> None:
    item = finding()
    draft = AnswerDraft(
        headline="利用状況の変化を確認しました",
        finding_ids=(item.id,),
        next_question="チャネル別に確認しますか？",
    )

    rendered = AnswerRenderer.render(draft, (item,))

    assert rendered == (
        "利用状況の変化を確認しました\n\n"
        "- active_users: 120（前月比 20%）\n\n"
        "チャネル別に確認しますか？"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("headline", "前月比20%です"),
        ("next_question", "上位3件を見ますか？"),
        ("headline", "売上は￥増加しました"),
    ],
)
def test_answer_draft_rejects_llm_numeric_or_currency_claims(
    field: str,
    value: str,
) -> None:
    payload = {
        "headline": "変化を確認しました",
        "finding_ids": (finding().id,),
        "next_question": None,
        field: value,
    }

    with pytest.raises(ValidationError, match="cannot contain numeric claims"):
        AnswerDraft.model_validate(payload)


def test_answer_renderer_rejects_unknown_finding_reference() -> None:
    draft = AnswerDraft(
        headline="変化を確認しました",
        finding_ids=("finding_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
    )

    with pytest.raises(ValueError, match="unknown_finding"):
        AnswerRenderer.render(draft, (finding(),))


def test_answer_composition_contract_needs_no_resource_scope() -> None:
    state = AnalysisState(
        data_source_set_id="company_a_web",
        semantic_version="a-web-v1",
        period=DatePeriod(start=date(2026, 8, 1), end=date(2026, 8, 24)),
    )

    assert "property" not in state.model_dump(mode="json")
    assert (
        "dataset_answer"
        not in AnswerDraft(
            headline="変化を確認しました",
            finding_ids=(finding().id,),
        ).model_dump_json()
    )
