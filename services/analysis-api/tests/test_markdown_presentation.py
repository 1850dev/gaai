from analytics_ai.agent.markdown import bounded_document, cell, prose
from analytics_ai.agent.models import AgentSummary, GroundedAssessment
from analytics_ai.agent.presentation import render_summary
from analytics_ai.agent.staged import render_progress
from analytics_ai.agent.staged_models import GroupedRequest, GroupItem, StagedPlan
from analytics_ai.domain.models import AnalysisMessage, AnalysisState
from tests.test_orchestrator import active_users_plan


def test_summary_is_structured_without_model_html_or_new_numbers() -> None:
    fact = {
        "id": "f",
        "text": "PV: 123",
        "metric": "page_views",
        "evidence_ids": ["evidence_original"],
        "quality_notes": ["result_row_limit_applied"],
    }
    summary = AgentSummary(
        explanation="取得済みの結果を整理します。",
        assessments=(
            GroundedAssessment(
                interpretation="【確認された事実】主要ページです。【改善仮説】導線を検証します。",
                fact_ids=("f",),
            ),
        ),
        limitations=("因果関係は不明です。",),
        questions=("回遊を確認しますか。",),
    )
    body = render_summary(summary, [fact], (), ())
    assert body.startswith("## 分析のまとめ\n\n")
    assert "**改善仮説**\n\n" in body
    assert "## 評価の前提・限界\n\n- 因果関係は不明です。" in body
    assert "PV: 123" in body
    assert "`evidence_original`" in body
    assert "result_row_limit_applied" not in body
    assert "## 次に確認できること\n\n- 回遊を確認しますか。" in body


def test_staged_tables_keep_each_page_and_its_evidence() -> None:
    staged = StagedPlan(
        objective="各ページの参照元",
        request=GroupedRequest(primary="ページ", secondary="参照元", limit=5),
        primary_field="pagePathPlusQueryString",
        secondary_field="sessionSource",
        metric_field="screenPageViews",
        rank_plan=active_users_plan(),
        anchor=AnalysisState(data_source_set_id="d", semantic_version="test-v1"),
        phase="complete",
        rank_evidence_ids=("evidence_rank",),
        items=tuple(
            GroupItem(
                value=f"/page-{i}/",
                total="123",
                status="complete",
                rows=(
                    {"sessionSource": "app|search\n<script>", "screenPageViews": "42"},
                ),
                evidence_ids=(f"evidence_child_{i}",),
            )
            for i in range(5)
        ),
    )
    body = render_progress(staged)
    assert body.count("| 参照元 | ページビュー数 |") == 5
    for i in range(5):
        assert f"### /page-{i}/" in body
        assert f"`evidence_child_{i}`" in body
    assert "| app\\|search \\<script\\> | 42 |" in body


def test_plain_message_compatibility_and_safe_document_boundaries() -> None:
    old = AnalysisMessage(
        id="msg_" + "a" * 32,
        request_id="r",
        session_id="s",
        user_id="u",
        company_id="c",
        data_source_set_id="d",
        role="assistant",
        content="古い\n回答",
    )
    assert old.content_format == "plain"
    assert old.content == "古い\n回答"
    assert cell("a|b\n![x](url)") == "a\\|b \\!\\[x\\](url)"
    assert prose("<script>**bad**") == "\\<script\\>\\*\\*bad\\*\\*"
    long = "## 結果\n\n" + "本文。\n\n" * 6000
    bounded = bounded_document(long)
    assert len(bounded) <= 16000
    assert "末尾を省略" in bounded
