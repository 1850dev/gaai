"""Explicit report intent and backward-compatible execution attribution."""

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from analytics_ai.agent.facts import _number
from analytics_ai.agent.markdown import cell as md_cell
from analytics_ai.agent.markdown import evidence, prose, text
from analytics_ai.agent.models import AgentSummary, Proposal
from analytics_ai.domain.models import AnalysisMessage, MessageRole
from analytics_ai.visualization.planner import (
    DIMENSION_LABELS,
    METRIC_LABELS,
    TIME_GRAIN_LABELS,
)

# A dependent analysis can contain the ranking plus up to ten entity results.
# Keep the rendering bounded without silently dropping the last target tables.
MAX_BREAKDOWN_FACTS = 11
MAX_BREAKDOWN_ROWS = 10


def _dimension_label(field: str) -> str:
    return DIMENSION_LABELS.get(field, TIME_GRAIN_LABELS.get(field, field))


def _fact_scope(fact: dict[str, Any]) -> str:
    """Keep each table independently attributable to its verified query scope."""
    parts: list[str] = []
    if fact.get("historical"):
        parts.append(f"過去の取得結果（保存: {fact.get('recorded_at', '不明')}）")
    if period := fact.get("period"):
        parts.append(f"{period['start']}〜{period['end']}")
    if perspective := fact.get("perspective"):
        parts.append(str(perspective))
    if filters := fact.get("filters"):
        parts.append(
            "絞込み: "
            + "、".join(
                f"{_dimension_label(f['field_name'])}={f['value']}" for f in filters
            )
        )
    return " / ".join(parts)


def report_intent(content: str) -> bool | None:
    # "追加取得は不要" must not negate a separate request for a report.
    explicit = False
    for clause in re.split(r"[。\n、，,;；!?！？]", content):
        if not re.search(r"レポート|報告書|\breport\b", clause, re.IGNORECASE):
            continue
        if re.search(
            r"(?:レポート|報告書|\breport\b)(?:化|作成|を作成|は|を|が|の|まだ|"
            r"今回|今は|今すぐ|\s){0,7}"
            r"(?:不要|いらない|しない|作らない|作らなく|作成しない|しなくて|"
            r"後で|必要ない|せず|禁止)|"
            r"(?:レポート|報告書)(?:を|は|に)?(?:まだ)?(?:作成|作り|まとめ)"
            r"(?:しない|しなく|ない|なく|ず|せず)",
            clause,
            re.IGNORECASE,
        ):
            return False
        explicit |= bool(
            re.search(
                r"作成|作って|作り|まとめ|にして|出力|生成|更新|修正|create|build",
                clause,
                re.IGNORECASE,
            )
        )
    if explicit:
        return True
    return None


def attributed_message(message: AnalysisMessage) -> AnalysisMessage:
    if message.kind == "agent_execution":
        return message
    # Recognize only the old server-generated execution IDs, not user prose.
    if message.role == MessageRole.USER and message.content.startswith("調査: "):
        for step in range(7):
            key = f"{message.request_id}:query:{step}"
            digest = hashlib.sha256(
                f"{message.session_id}\0user\0{key}".encode()
            ).hexdigest()
            if message.id == f"msg_{digest[:32]}":
                return message.model_copy(
                    update={"role": MessageRole.ASSISTANT, "kind": "agent_execution"}
                )
    return message


def error_label(code: str) -> str:
    for phase, label in {
        "selection": "次の行動の選択",
        "ga4_arguments": "GA4分析条件の生成",
        "advice": "改善提案の生成",
    }.items():
        if code == f"agent_{phase}_timeout":
            return f"{label}が応答期限に達しました。取得済みの根拠は保存されています"
        if code == f"agent_{phase}_invalid_schema":
            return f"{label}を再生成しましたが、入力形式の検証を通過できませんでした"
    if code.startswith("requested_analysis_not_observed:"):
        return "指定された指標・集計軸・期間の根拠が不足しているため、部分的な回答です"
    if code.startswith("followup_context_changed:"):
        return (
            "計画が指定された変更内容・保持条件に合わないため、"
            "その計画のデータ取得を行いませんでした"
        )
    if code.startswith("semantic_"):
        return f"指標・集計軸の登録定義を解決できませんでした（{code}）"
    labels = {
        "grouped_analysis_requires_two_dimensions_one_metric": (
            "段階分析には登録済みの集計軸を二つ、順位を決める指標を一つ指定してください"
        ),
        "grouped_analysis_single_period_required": (
            "段階分析は現在、単一期間に対応しています。まず対象期間を一つ指定してください"
        ),
        "grouped_analysis_requires_core_report": (
            "この段階分析は通常レポート向けです。リアルタイムやファネルとは分けてください"
        ),
        "grouped_result_missing_exact_value": (
            "対象を特定できる完全な集計値が不足しているため、内訳の自動調査を停止しました"
        ),
        "mcp_result_row_limit_exceeded": (
            "取得結果がアプリの返却件数上限を超えたため、その調査は採用していません。"
            "期間・集計軸を絞った追加調査が必要です"
        ),
        "ga4_metric_dimension_incompatible": "GA4で指標と集計軸の組合せが非互換でした",
        "invalid_schema_or_generated_numbers": (
            "AIの計画が形式・数値表現の検証を通りませんでした"
        ),
        "duplicate_query_choose_another_analysis_or_finish": (
            "同じ調査の繰り返しを防止しました"
        ),
        "agent_summary_unavailable": "取得結果のAIによる文章化に失敗しました",
        "agent_advice_unavailable": (
            "改善提案を検証できませんでした。取得済みの根拠は保持されています"
        ),
        "agent_advice_grounding_failed": (
            "改善提案を修正しましたが、参照元の解釈・計測の前提・対応する分析機能との"
            "整合性を確認できなかったため、その提案は採用していません。"
            "GA4 APIの取得上限ではありません"
        ),
        "agent_summary_grounding_failed": (
            "回答を修正しましたが、根拠や実行可能な分析との整合性を確認できなかったため、"
            "その文章は採用せず、取得済みの根拠を表示しています"
        ),
        "agent_model_rate_limited": (
            "Geminiが利用量または処理容量の制限（HTTP 429）を返しました。"
            "GA4 APIの取得上限とは別です。取得済みの結果は保持しています"
        ),
        "agent_planner_timeout": "AIの計画生成が時間内に完了しませんでした",
        "agent_planner_unavailable": (
            "AIの計画サービスでエラーが発生しました。"
            "GA4 APIの利用上限と断定できるエラーではありません"
        ),
        "agent_tool_failed": (
            "分析ツールの処理でエラーが発生しました。保存済みの結果は維持しています"
        ),
        "ga4_query_repair_rejected": (
            "指定された条件を維持できる別の取得計画を確認できなかったため、再取得しませんでした"
        ),
        "application_time_budget": "追加調査がアプリの制限時間内に完了しませんでした",
        "analysis_timeout": (
            "個別の分析が処理期限に達しました。取得済みの結果は保存されています"
        ),
    }
    return labels.get(
        code, f"処理を完了できませんでした（{code}）。原因の追加確認が必要です"
    )


def render_summary(
    summary: AgentSummary | None,
    facts: list[dict[str, Any]],
    targets: tuple[str, ...],
    completed: tuple[str, ...],
    *,
    include_breakdowns: bool = True,
    proposals: tuple[Proposal, ...] = (),
) -> str:
    """Insert only server-validated facts, with scope and evidence attribution."""
    by_id = {fact["id"]: fact for fact in facts}
    lines: list[str] = []
    cited: set[str] = set()

    def fact_line(fact: dict[str, Any]) -> str:
        scope = _fact_scope(fact)
        scope = f"{scope} / " if scope else ""
        return (
            f"- {text(scope)}{text(fact.get('formatted_text', fact['text']))}"
            f"（{evidence(fact['evidence_ids'])}）"
        )

    if summary:
        lines.extend(("## 分析のまとめ", "", prose(summary.explanation)))
        if summary.assessments:
            lines.extend(("", "## 解釈・改善仮説（数値の根拠とは区別）", ""))
        for index, assessment in enumerate(summary.assessments, 1):
            # Unknown references must be rejected before this function is called.
            if not all(fid in by_id for fid in assessment.fact_ids):
                raise ValueError("summary_unknown_fact_reference")
            lines.extend(
                ("", f"### 観点 {index}", "", prose(assessment.interpretation), "")
            )
            for fid in assessment.fact_ids:
                if fid not in cited:
                    lines.append(fact_line(by_id[fid]))
                    cited.add(fid)
        if summary.limitations:
            lines.extend(
                (
                    "",
                    "## 評価の前提・限界",
                    "",
                    *(f"- {text(v)}" for v in summary.limitations),
                )
            )
    else:
        lines.append(
            "取得済みデータに基づく確認結果です。"
            if facts
            else "今回は確認済みの分析データを取得できませんでした。"
        )
        # A deterministic assessment still works when text generation is down.
        # Only period-wide KPI changes can support this statement; no summing UU.
        for fact in facts:
            if (
                fact.get("dimension_field") is not None
                or fact.get("change_rate") is None
            ):
                continue
            try:
                change = Decimal(fact["change_rate"])
                direction = "増加" if change > 0 else "減少" if change < 0 else "横ばい"
                lines.append(
                    f"{fact.get('label', fact['metric'])}は"
                    f"比較期間から{direction}しています。"
                )
            except InvalidOperation:
                continue
        if facts:
            lines.append(
                "事業目標の達成や変化の原因までは、この数値だけでは判断できません。"
            )
    # Preserve accepted proposal caveats even when the model summary omits them.
    # The caller supplies verified proposals; treat them as hypotheses, not facts.
    evaluated = [p for p in proposals if p.evaluation is not None]
    if evaluated:
        lines.extend(("", "## 改善仮説の評価方法（未検証）", ""))
    known_evidence = {eid for fact in facts for eid in fact["evidence_ids"]}
    for proposal in evaluated:
        if not set(proposal.evidence_ids).issubset(known_evidence):
            raise ValueError("proposal_unknown_evidence_reference")
        evaluation = proposal.evaluation
        assert evaluation is not None
        position = (
            "代理指標（事業成果そのものではありません）"
            if evaluation.measurement_status == "proxy"
            else "計測・定義の確認が必要"
        )
        lines.extend(
            (
                f"### {text(proposal.title)}",
                "",
                f"- 仮説: {text(proposal.hypothesis)}",
                f"- 変更案: {text(proposal.changes)}",
                f"- 目的との関係: {text(evaluation.goal_alignment)}",
                f"- 評価指標候補: {text(proposal.success_metric or '未確定')}",
                f"- 指標を選ぶ理由: {text(evaluation.metric_reason)}",
                f"- 指標の限界: {text(evaluation.metric_limitations)}",
                f"- 計測の位置付け: {position}",
                *(f"- 実施前の確認: {text(p)}" for p in evaluation.prerequisites),
                f"- 検証方法: {text(proposal.validation_plan)}",
                evidence(proposal.evidence_ids),
                "",
            )
        )
        for check in evaluation.goal_checks:
            basis = {
                "user_request": "今回の依頼",
                "site_context": "サイト設定",
                "user_memory": "ユーザー発言・確認済みの合意",
                "unconfirmed": "仮の目的（未確認）",
            }[check.goal_basis]
            state = {
                "proxy_only": "代理指標のみ。目的達成は未確認",
                "unmeasured": "必要な根拠は未取得・未計測",
                "goal_unconfirmed": "事業目的の確認が必要",
            }[check.evidence_status]
            kind = {
                "registered_analysis": "登録済みデータでの分析候補（自動実行しません）",
                "measurement_check": "計測・定義を担当者に確認",
                "business_question": "事業上の意図を確認",
                "human_review": "担当者による確認・調査",
            }[check.next_check_kind]
            lines.extend(
                (
                    f"#### 評価したい目的: {text(check.goal)}",
                    "",
                    f"- 目的の出典: {basis}"
                    + (f"「{text(check.goal_quote)}」" if check.goal_quote else ""),
                    f"- 必要な根拠: {text(check.required_evidence)}",
                    f"- 現在の確認状況: {state}",
                    f"- まだ分からないこと: {text(check.gap)}",
                    f"- 次の確認（{kind}）: {text(check.next_check)}",
                    "",
                )
            )
    uncited = [f for f in facts if f["id"] not in cited]
    if uncited:
        lines.extend(("", "## 確認した数値", "", *(fact_line(f) for f in uncited[:12])))
    breakdowns = [
        fact
        for fact in facts
        if fact.get("category_values") or fact.get("category_comparisons")
    ]
    shown_breakdowns = breakdowns[:MAX_BREAKDOWN_FACTS] if include_breakdowns else []
    if shown_breakdowns:
        lines.extend(("", "## 取得済みの内訳（確認できた事実）", ""))
    for fact in shown_breakdowns:
        if fact.get("category_comparisons"):
            continue
        label = fact.get("label", fact["metric"])
        axes = " × ".join(_dimension_label(f) for f in fact.get("dimension_fields", []))
        lines.extend(
            (
                "",
                f"### {text(axes)}：{text(METRIC_LABELS.get(label, label))}"
                "（取得範囲の組合せ）",
                "",
            )
        )
        if scope := _fact_scope(fact):
            lines.extend((text(scope), ""))
        lines.extend(
            (
                f"| {md_cell(axes) or '項目'} | "
                f"{md_cell(METRIC_LABELS.get(label, label))} |",
                "| --- | ---: |",
            )
        )
        for cell in fact["category_values"][:MAX_BREAKDOWN_ROWS]:
            category = " / ".join(cell["dimensions"].values())
            lines.append(f"| {md_cell(category)} | {_number(cell['value'])} |")
        lines.extend(("", evidence(fact["evidence_ids"]), ""))
        if len(fact["category_values"]) > MAX_BREAKDOWN_ROWS:
            lines.append(
                f"本文では取得済み内訳の先頭{MAX_BREAKDOWN_ROWS}件を表示しています。"
            )
    # Do not hide the requested breakdown behind a single representative KPI,
    # including when the model's qualitative synthesis fails.
    for fact in shown_breakdowns:
        if not fact.get("category_comparisons"):
            continue
        label = fact.get("label", fact["metric"])
        label = METRIC_LABELS.get(label, label)
        lines.extend(("", f"### {text(label)}の内訳比較（取得範囲内）", ""))
        if scope := _fact_scope(fact):
            lines.extend((text(scope), ""))
        period, baseline = fact.get("period"), fact.get("comparison_period")
        if period and baseline:
            lines.append(
                f"比較元 {baseline['start']}〜{baseline['end']} → "
                f"対象 {period['start']}〜{period['end']}"
            )
        axis = " × ".join(_dimension_label(f) for f in fact.get("dimension_fields", []))
        lines.extend(
            (
                "",
                f"| {md_cell(axis) or '項目'} | 比較基準 | 対象 | "
                "差（対象 − 基準） | 増減率 |",
                "| --- | ---: | ---: | ---: | ---: |",
            )
        )
        for row in fact["category_comparisons"][:MAX_BREAKDOWN_ROWS]:
            category = " / ".join(str(v) for v in row["dimensions"].values())
            current = (
                _number(row["current_value"])
                if row["current_value"] is not None
                else "未確認"
            )
            before = (
                _number(row["comparison_value"])
                if row["comparison_value"] is not None
                else "未確認"
            )
            delta, rate = "—", "—"
            if row.get("status") == "matched" and row["difference"] is not None:
                delta = _number(row["difference"])
            if row.get("status") == "matched" and row["change_rate"] is not None:
                percentage = str(Decimal(row["change_rate"]) * 100)
                rate = f"{_number(percentage, percentage=True)}%"
            lines.append(
                f"| {md_cell(category[:160])} | {before} | {current} | "
                f"{delta} | {rate} |"
            )
        lines.extend(("", evidence(fact["evidence_ids"]), ""))
        if len(fact["category_comparisons"]) > MAX_BREAKDOWN_ROWS:
            lines.append(
                f"本文では取得済み比較内訳の先頭{MAX_BREAKDOWN_ROWS}件を表示しています。"
            )
    if len(breakdowns) > MAX_BREAKDOWN_FACTS:
        lines.append(
            f"本文では取得済み内訳のうち{MAX_BREAKDOWN_FACTS}分析を表示しています。"
            "省略した内訳は未取得ではありません。"
        )
    quality = list(
        dict.fromkeys(
            note
            for fact in facts
            for note in (*fact.get("assumptions", ()), *fact.get("quality_notes", ()))
        )
    )
    if quality:
        labels = {
            "result_row_limit_applied": (
                "取得件数に上限を適用しています。表示された内訳は全件ではありません。"
            )
        }
        lines.extend(
            (
                "",
                "## データ上の注意",
                "",
                *(f"- {text(labels.get(v, v))}" for v in quality[:8]),
            )
        )
    missing = [target for target in targets if target not in completed]
    if missing:
        lines.extend(("", "未確認の観点: " + text("、".join(missing))))
    if summary and summary.questions:
        lines.extend(
            (
                "",
                "## 次に確認できること",
                "",
                *(f"- {text(v)}" for v in summary.questions),
            )
        )
    elif missing:
        lines.append(
            f"次は{'・'.join(missing)}を個別に調査し、今回の結果と照合できます。"
        )
    elif not summary:
        lines.append("次に、流入チャネルや主要ページごとの変化を深掘りできます。")
    return "\n".join(lines)
