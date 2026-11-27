"""Opt-in site-understanding evaluation with public context and synthetic evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from time import monotonic
from typing import Any

from evaluation.live_continuation import MAY, Budget, SeedDirector, build, emit, turn

from analytics_ai.agent.director import VertexAgentDirector, ga4_argument_context
from analytics_ai.agent.models import AgentDecision, AgentSummary, SiteContext
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.site_context import site_understanding
from analytics_ai.config import Settings
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.mcp_gateway.gateway import PreparedInvocation
from analytics_ai.orchestrator.planning import IntentPlan

PLAN_QUESTION = (
    "2026年5月のこのサイトを評価するため、最初に何を調べるべきか決めてください。"
    "レポートは不要です。"
)
ADVICE_QUESTION = (
    "取得済みの結果を使って、このサイトで優先する改善案を考えてください。"
    "レポートは不要です。"
)


def purpose_variants(proposed: SiteContext) -> tuple[tuple[str, SiteContext], ...]:
    """Counterfactual changes GOALS only; never saved to the real application."""
    discovery = proposed.model_copy(
        update={
            "goals": (
                "【比較検証だけの仮想目的。実際の札幌店の方針ではない】"
                "店舗公式サイトで催事・売場の魅力を発見してもらい、関連する読み物への関心と"
                "再訪を育てる。必要な案内を素早く見つけることに加え、まだ知らない催事や"
                "売場との出会いを重視する。効率的な用件完了とは異なる評価観点を考えたい。"
                "閲覧規模は登録済みPV・ユーザー・セッションで把握し、エンゲージメントや"
                "再訪に関する登録指標を候補に、その定義と取得可否を確認する。"
                "PVや滞在の増加だけでは内容への関心を証明しない。迷い・再読込との区別、"
                "読後の意見や再訪などの計測の有無を確認して評価したい。"
                "目標値、実来店、満足度、会員化、売上の実測は未確認。CVやイベントを"
                "勝手に定義せず、確認できる数値と未検証の仮説を分ける。"
            )
        }
    )
    return (("blank", SiteContext()), ("sapporo", proposed), ("discovery", discovery))


def with_site_context(
    base: tuple[dict[str, Any], dict[str, Any], dict[str, Any]], site: SiteContext
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Freeze question, facts, IDs, history and catalog across all conditions."""
    result = deepcopy(base)
    for context in result:
        context["site_context"] = site_understanding(
            site.model_copy(update={"revision": 1})
        )
    return result


def non_site_fingerprint(context: dict[str, Any]) -> str:
    data = {key: value for key, value in context.items() if key != "site_context"}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class PageExecutor:
    """Deliberately synthetic values."""

    async def execute(self, invocation: PreparedInvocation) -> ToolExecutionResult:
        assert invocation.data_source_set_id == "qa_synthetic_web"
        assert invocation.tool_name == "run_report"
        assert invocation.arguments["dimensions"] == [
            "pagePathPlusQueryString",
            "sessionSource",
        ]
        assert invocation.arguments["metrics"] == ["screenPageViews"]
        rows = [
            {
                "pagePathPlusQueryString": page,
                "sessionSource": source,
                "screenPageViews": value,
            }
            for page, source, value in (
                ("/store/event/", "google", 120),
                ("/store/event/", "(direct)", 60),
                ("/store/floor/", "google", 90),
                ("/store/access.html", "google", 80),
                ("/store/", "mobile_app_referral", 100),
            )
        ]
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )


class CaptureDirector(SeedDirector):
    def __init__(self, seed: bool = False) -> None:
        super().__init__()
        self.seed = seed
        self.context: dict[str, Any] = {}
        self.summary_input: dict[str, Any] = {}

    async def decide(self, context: dict[str, Any]) -> AgentDecision:
        self.context = context
        if self.seed:
            self.seed = False
            return AgentDecision(
                action="query",
                objective="合成データ準備",
                explanation="合成ページ集計を準備します。",
                plan=IntentPlan(
                    intent="合成ページ集計",
                    period=MAY,
                    metric_terms=("page_views",),
                    dimension_terms=("page_path", "session_source"),
                ),
            )
        return AgentDecision(
            action="ask",
            objective="検証用の文脈取得",
            explanation="評価用の文脈を確認しました。",
            questions=("検証を進めますか？",),
        )

    async def summarize(self, context: dict[str, Any]) -> AgentSummary:
        self.summary_input = context
        return await super().summarize(context)


class AuditedVertex(VertexAgentDirector):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.stages: list[dict[str, Any]] = []

    async def _content(
        self, *, phase: str = "legacy", repair_attempt: int = 0, **kwargs: Any
    ) -> Any:
        context = json.loads(kwargs["contents"])
        response = await super()._content(
            phase=phase, repair_attempt=repair_attempt, **kwargs
        )
        row: dict[str, Any] = {
            "phase": phase,
            "repair_attempt": repair_attempt,
            "site_context_present": "site_context" in context,
        }
        if phase == "selection":
            try:
                selected = json.loads(response.text or "{}")
                row["tool"] = selected.get("tool")
                row["analysis_brief"] = selected.get("analysis_brief")
            except ValueError:
                row["invalid_json"] = True
        if phase.endswith("_grounding"):
            try:
                row["review"] = json.loads(response.text or "{}")
            except ValueError:
                row["invalid_json"] = True
        self.stages.append(row)
        return response


async def contexts(
    site: SiteContext,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    # Same application save/read and context construction as /settings, but memory-only.
    agent, _, _ = await build()
    saved = await agent.save_site("qa-user", "qa_synthetic_web", site)
    assert await agent.site("qa-user", "qa_synthetic_web") == saved
    capture = CaptureDirector()
    agent.director = capture
    await turn(agent, PLAN_QUESTION, "plan-context")
    planning = capture.context

    agent, _, _ = await build()
    saved = await agent.save_site("qa-user", "qa_synthetic_web", site)
    capture = CaptureDirector(seed=True)
    agent.director = capture
    assert agent.orchestrator is not None
    agent.orchestrator._executor = PageExecutor()
    # Finish after the one synthetic query so actual summary context is assembled.
    original_decide = capture.decide

    async def seed_decide(context: dict[str, Any]) -> AgentDecision:
        if capture.seed:
            return await original_decide(context)
        return AgentDecision(
            action="finish", objective="合成データ準備", explanation="合成結果です。"
        )

    capture.decide = seed_decide  # type: ignore[method-assign]
    seeded = await turn(agent, "2026年5月のページと参照元別PV", "seed")
    assert seeded[-1]["data"]["status"] == "complete"
    summary = {**capture.summary_input, "user_question": ADVICE_QUESTION}
    capture.decide = original_decide  # type: ignore[method-assign]
    await turn(agent, ADVICE_QUESTION, "advice-context")
    assert capture.context["site_context"] == site_understanding(saved)
    assert summary["site_context"] == site_understanding(saved)
    return planning, capture.context, summary


async def evaluate(
    profile: Path,
    live_enabled: bool,
    *,
    variant: str | None = None,
    selected_phase: str | None = None,
    suite: str = "legacy",
    repetitions: int = 1,
) -> None:
    if suite not in {"legacy", "purposes"} or not 1 <= repetitions <= 2:
        raise ValueError("invalid_evaluation_suite_or_repetitions")
    proposed = SiteContext.model_validate_json(
        await asyncio.to_thread(profile.read_text)
    )
    strict = proposed.model_copy(
        update={
            "technical_constraints": (
                "検証用反転条件: 今期は全ページの変更・新規制作・リライト不可。"
                "観測と担当者への確認だけを行う。"
            ),
            "content_policy": (
                "検証用反転条件: 新規制作もリライトも不可。改善案の実行を勧めず、"
                "計測上の限界と担当者への確認事項のみ提示する。"
            ),
            "allowed_surfaces": (),
            "allow_new_content": False,
            "allow_rewrite": False,
        }
    )
    settings = Settings()
    if live_enabled and settings.environment not in {"local", "dev"}:
        raise RuntimeError("live_evaluation_requires_local_or_dev")
    live = AuditedVertex(settings) if live_enabled else None
    # Six runs now include bounded proposal AND summary quality reviews/repairs.
    # Evaluation-only ceiling; product phase deadlines/retry limits are unchanged.
    maximum = 72 if suite == "purposes" else 32
    budget = Budget(maximum=maximum)
    client = budget.bind(live) if live else None
    emit(
        {
            "kind": "start",
            "model": live.model if live else "self-test",
            "max_model_calls": maximum,
            "suite": suite,
            "repetitions": repetitions,
            "scope": "public_site_context_and_synthetic_facts",
            "real_ga4_calls": 0,
            "persistent_writes": 0,
        }
    )
    try:
        variants = (
            purpose_variants(proposed)
            if suite == "purposes"
            else (
                ("blank", SiteContext()),
                ("sapporo", proposed),
                ("strict_no_changes", strict),
                ("sapporo_repeat", proposed),
            )
        )
        # Prepare ONCE, then vary only site_context, not random IDs or timestamps.
        base = await contexts(SiteContext()) if suite == "purposes" else None
        runs = [
            (repeat, label, site)
            for repeat in range(1, repetitions + 1)
            for label, site in (variants if repeat == 1 else tuple(reversed(variants)))
        ]
        for repeat, label, site in runs:
            if variant is not None and label != variant:
                continue
            planning, advice, summary = (
                with_site_context(base, site)
                if base is not None
                else await contexts(site)
            )
            emit(
                {
                    "kind": "wiring",
                    "variant": label,
                    "repetition": repeat,
                    "non_site_fingerprints": [
                        non_site_fingerprint(c) for c in (planning, advice, summary)
                    ],
                    "saved_and_loaded": base is None,
                    "site_context_projected_onto_frozen_inputs": base is not None,
                    "selection_has_site": "site_context" in planning,
                    "arguments_have_site": "site_context"
                    in ga4_argument_context(planning),
                    "summary_has_site": "site_context" in summary,
                    "fact_count": len(summary["facts"]),
                }
            )
            if not live:
                continue
            for phase, context in (
                ("plan", planning),
                ("advice", advice),
                ("summary", summary),
            ):
                if selected_phase is not None and phase != selected_phase:
                    continue
                live.stages = []
                started, before = monotonic(), budget.calls
                try:
                    async with asyncio.timeout(100 if phase != "summary" else 55):
                        result = await (
                            live.summarize(context)
                            if phase == "summary"
                            else live.decide(context)
                        )
                    gate = "not_applicable"
                    if isinstance(result, AgentDecision):
                        metrics = {
                            d["term"]
                            for d in context["catalog"]
                            if d["kind"] == "metric"
                        }
                        try:
                            AgentService.validate_decision(
                                result, site, context["facts"], metrics
                            )
                            gate = "accepted"
                        except ValueError as error:
                            gate = str(error)
                        if phase == "advice" and gate == "accepted":
                            summary["proposals"] = [
                                p.model_dump(mode="json") for p in result.proposals
                            ]
                    emit(
                        {
                            "kind": "result",
                            "variant": label,
                            "repetition": repeat,
                            "phase": phase,
                            "latency_seconds": round(monotonic() - started, 2),
                            "model_calls": budget.calls - before,
                            "proposal_guard": gate,
                            "output": result.model_dump(mode="json"),
                            "summary_proposal_count": len(context.get("proposals", []))
                            if phase == "summary"
                            else None,
                            "stages": live.stages,
                        }
                    )
                except Exception as error:
                    emit(
                        {
                            "kind": "error",
                            "variant": label,
                            "repetition": repeat,
                            "phase": phase,
                            "error_type": type(error).__name__,
                            "error_code": getattr(error, "error_code", None),
                            "validation_feedback": getattr(error, "feedback", None),
                            "evaluation_budget_exhausted": budget.calls >= maximum,
                            "provider_status": getattr(error, "code", None),
                            "latency_seconds": round(monotonic() - started, 2),
                            "model_calls": budget.calls - before,
                            "stages": live.stages,
                        }
                    )
    finally:
        if client:
            await client.aio.aclose()
        emit(
            {
                "kind": "end",
                "model_calls": budget.calls,
                "reported_tokens": budget.tokens,
                "real_ga4_calls": 0,
                "persistent_writes": 0,
            }
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--variant",
        choices=(
            "blank",
            "sapporo",
            "strict_no_changes",
            "sapporo_repeat",
            "discovery",
        ),
    )
    parser.add_argument("--phase", choices=("plan", "advice", "summary"))
    parser.add_argument("--suite", choices=("legacy", "purposes"), default="legacy")
    parser.add_argument("--repetitions", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    async with asyncio.timeout(900):
        await evaluate(
            args.profile,
            args.live,
            variant=args.variant,
            selected_phase=args.phase,
            suite=args.suite,
            repetitions=args.repetitions,
        )


if __name__ == "__main__":
    asyncio.run(main())
