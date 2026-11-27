"""Opt-in real-model quality checks against synthetic bad AND valid statements."""

import argparse
import asyncio
from copy import deepcopy
from time import monotonic
from typing import Any

from evaluation.live_continuation import Budget, emit
from pydantic import ValidationError

from analytics_ai.agent.director import VertexAgentDirector
from analytics_ai.agent.grounding import GroundingRejected
from analytics_ai.agent.models import AgentDecision, Proposal
from analytics_ai.config import Settings


def cases() -> list[tuple[str, str | None, dict[str, Any], AgentDecision]]:
    base: dict[str, Any] = {
        "user_question": "取得済みのデータから改善案を考えて。追加取得はしないで。",
        "data_acquisition_allowed": False,
        "site_context": {"role": "background_not_policy", "site_basics": "店舗案内"},
        "catalog": [{"term": "page_views", "kind": "metric"}],
        "analysis_capabilities": {
            "methods": {"funnel": {"registered_funnels": []}},
            "unavailable": [{"feature": "raw_user_journey_or_crm_join"}],
        },
        "facts": [
            {
                "id": "f",
                "evidence_ids": ["e"],
                "metric": "page_views",
                "dimension_fields": ["pagePath", "sessionSource"],
                "category_values": [
                    {
                        "dimensions": {
                            "pagePath": "/",
                            "sessionSource": "dm_passport_app",
                        },
                        "value": "100",
                    },
                    {
                        "dimensions": {
                            "pagePath": "/event/",
                            "sessionSource": "google",
                        },
                        "value": "120",
                    },
                ],
            }
        ],
    }
    samples = [
        (
            "app_title_with_caveat",
            "source_identity_inference",
            "アプリ等外部連携流入向けのトップページ案内最適化",
            "",
            "",
        ),
        (
            "organic_from_name",
            "source_channel_inference",
            "案内改善",
            "googleからの自然検索流入が多いので案内を改善する仮説です。",
            "",
        ),
        (
            "engagement_as_clicks",
            "metric_meaning_mismatch",
            "案内改善",
            "",
            "エンゲージメント率で有効な操作や案内リンクのクリック増加を測ります。",
        ),
        (
            "path_before_instrumentation",
            "measurement_prerequisite_missing",
            "案内改善",
            "",
            "遷移状況を比較し、必要に応じてクリック計測を追加検証します。",
        ),
        (
            "valid_labels_and_prerequisites",
            None,
            "参照元名ごとの案内改善",
            "",
            "まず担当者とイベント定義・計測実装・対応Toolを確認します。今は遷移は評価できません。",
        ),
    ]
    output = []
    for label, expected, title, hypothesis, validation in samples:
        candidate = AgentDecision(
            action="finish",
            objective="案内改善",
            explanation="改善仮説です。",
            proposals=(
                Proposal(
                    title=title,
                    hypothesis=hypothesis
                    or (
                        "参照元名 dm_passport_app と記録された閲覧があります。"
                        "利用者属性は不明です。"
                    ),
                    changes="案内の見出しを分かりやすくする案です。",
                    surfaces=("home",),
                    validation_plan=validation or "担当者に利用時の課題を確認します。",
                    evidence_ids=("e",),
                ),
            ),
            assumptions=("参照元名だけでは会員・アプリ利用者とは判断できません。",),
        )
        output.append((label, expected, deepcopy(base), candidate))
    valid = output[-1][3]
    for label, expected, question in (
        (
            "raw_path_offer",
            "unsupported_analysis_offer",
            "次に閲覧したページの遷移分析を進めますか？",
        ),
        (
            "tracking_write_offer",
            "unsupported_analysis_offer",
            "クリックイベント計測を追加設定しますか？",
        ),
        (
            "human_definition_check",
            None,
            "計測担当者に現在のイベント定義を確認できますか？",
        ),
    ):
        output.append(
            (
                label,
                expected,
                deepcopy(base),
                valid.model_copy(update={"questions": (question,)}),
            )
        )
    same = deepcopy(base)
    same["facts"][0]["dimension_fields"].append("sessionMedium")
    same["facts"][0]["category_values"][1]["dimensions"]["sessionMedium"] = "organic"
    organic = output[1][3]
    output.append(("same_cell_organic_supported", None, same, organic))
    other = deepcopy(base)
    other["facts"].append(
        {
            "id": "other",
            "dimension_fields": ["sessionMedium"],
            "category_values": [
                {"dimensions": {"sessionMedium": "organic"}, "value": "3"}
            ],
        }
    )
    output.append(
        ("other_fact_not_support", "source_channel_inference", other, organic)
    )
    return output


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    samples = cases()
    if not args.live:
        emit({"kind": "self_test", "cases": len(samples), "model_calls": 0})
        return
    settings = Settings()
    if settings.environment not in {"local", "dev"}:
        raise RuntimeError("live_evaluation_requires_local_or_dev")
    director = VertexAgentDirector(settings)
    budget = Budget(maximum=14)
    client = budget.bind(director)
    passed = 0
    try:
        async with asyncio.timeout(180):
            for label, expected, context, candidate in samples:
                started = monotonic()
                issues: list[dict[str, Any]] = []
                error_type = None
                validation_errors = None
                try:
                    async with asyncio.timeout(20):
                        await director._review_grounding(
                            context, candidate, "evaluation", 0
                        )
                except GroundingRejected as error:
                    import json

                    issues = json.loads(error.feedback)["issues"]
                except Exception as error:
                    error_type = type(error).__name__
                    if isinstance(error, ValidationError):
                        validation_errors = error.errors(
                            include_input=False,
                            include_context=False,
                            include_url=False,
                        )
                codes = {issue["code"] for issue in issues}
                ok = not error_type and (expected in codes if expected else not codes)
                passed += int(ok)
                emit(
                    {
                        "kind": "case",
                        "case": label,
                        "expected": expected,
                        "passed": ok,
                        "issues": issues,
                        "error_type": error_type,
                        "validation_errors": validation_errors,
                        "latency_seconds": round(monotonic() - started, 2),
                    }
                )
    finally:
        await client.aio.aclose()
        emit(
            {
                "kind": "end",
                "passed": passed,
                "cases": len(samples),
                "model_calls": budget.calls,
                "reported_tokens": budget.tokens,
                "real_ga4_calls": 0,
                "persistent_writes": 0,
            }
        )


if __name__ == "__main__":
    asyncio.run(main())
