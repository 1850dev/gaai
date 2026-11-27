import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from analytics_ai.agent.browser_director import _model_reply
from analytics_ai.agent.director import Ga4ToolCall
from analytics_ai.agent.explicit_comparison import explicit_comparison_table
from analytics_ai.agent.workflows import ToolSelection, explicit_semantic_fields
from analytics_ai.api.app import create_app
from analytics_ai.config import Settings
from analytics_ai.mcp_gateway.executor import McpExecutionDenied, ToolExecutionResult
from tests.test_cross_breakdown import register
from tests.test_orchestrator import StaticVerifier, add_session, build_orchestrator


def test_browser_selection_accepts_one_formatted_object_and_preserves_validation():
    output = (
        "<think>内部推論</think>\n"
        "```json\n"
        '{"tool":"ga4.analyze","workflow_id":"analysis.exploration.v1"}\n'
        "```"
    )
    context = {"user_question": "ページ別のPVを取得"}
    result = ToolSelection.model_validate_json(
        _model_reply("selection", output, context)
    )
    assert result.objective == context["user_question"]
    assert result.tool == "ga4.analyze"
    for invalid in (output + "{}", '[{"tool":"ga4.analyze"}]', "<think>{}"):
        with pytest.raises(ValueError):
            ToolSelection.model_validate_json(
                _model_reply("selection", invalid, context)
            )


def test_explicit_months_resolve_conflicting_local_model_relative_label():
    prompt = "2026年5月と6月のセッション数を比較し、増減の背景も考察してください。"
    model_plan = {
        "intent": "比較",
        "metric_terms": ["sessions"],
        "dimension_terms": ["session_source"],
        "period": {"start": "2026-06-01", "end": "2026-06-30"},
        "comparison_period": {"start": "2026-05-01", "end": "2026-05-31"},
        "comparison": "previous_month",
    }
    payload = {"plan": model_plan, "observed_filters": []}
    raw = json.dumps(payload)
    with pytest.raises(
        ValueError, match="explicit_and_relative_comparison_are_mutually_exclusive"
    ):
        Ga4ToolCall.model_validate_json(raw)
    fixed = _model_reply("ga4_arguments", raw, {"user_question": prompt})
    assert Ga4ToolCall.model_validate_json(fixed).plan.comparison == "none"
    assert _model_reply("ga4_arguments", raw, {"user_question": "前月と比較"}) == raw
    assert (
        _model_reply(
            "ga4_arguments", raw, {"user_question": prompt, "background": True}
        )
        == raw
    )
    model_plan["comparison_period"] = {"start": "invalid", "end": "2026-05-31"}
    with pytest.raises(ValueError, match=r"comparison_period\.start"):
        Ga4ToolCall.model_validate_json(
            _model_reply(
                "ga4_arguments", json.dumps(payload), {"user_question": prompt}
            )
        )


def test_browser_agent_uses_shared_cross_breakdown_and_staged_workflow(repository):
    session = asyncio.run(add_session(repository))
    fields = register(repository)
    definitions = list(repository.semantic_definitions.values())
    assert set(
        explicit_semantic_fields(
            "ページとセッションの参照元ごとのPVとUU",
            definitions,
            kind="metric",
        )
    ) == {"screenPageViews", "activeUsers"}
    assert set(
        explicit_semantic_fields(
            "セッションの参照元ごとのセッション数とUU",
            definitions,
            kind="metric",
        )
    ) == {"sessions", "activeUsers"}
    orchestrator, executor = build_orchestrator(repository, [], {})
    calls = []
    faults: list[str] = []

    async def execute(invocation):
        calls.append(invocation)
        if faults:
            raise McpExecutionDenied(faults.pop(0))
        assert invocation.arguments["property_id"] == "222222222"
        dimensions = invocation.arguments["dimensions"]
        metrics = invocation.arguments["metrics"]
        rows = [
            {
                **{axis: f"observed-{axis}-{i}" for axis in dimensions},
                **{metric: str(15 - i) for metric in metrics},
            }
            for i in range(5)
        ]
        if len(invocation.arguments.get("date_ranges", ())) == 2:
            rows = [
                {"sessionSource": "google", "sessions": "120", "dateRange": "current"},
                {
                    "sessionSource": "google",
                    "sessions": "100",
                    "dateRange": "comparison",
                },
                {
                    "sessionSource": "new-source",
                    "sessions": "10",
                    "dateRange": "current",
                },
            ]
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )

    executor.execute = execute
    app = create_app(
        Settings(
            environment="test",
            enable_analytics_tools=True,
            enable_browser_qwen=True,
            enable_browser_agent=True,
        ),
        repository=repository,
        token_verifier=StaticVerifier(),
        orchestrator=orchestrator,
    )
    api = TestClient(app)
    path = f"/api/sessions/{session.id}"
    options = {"headers": {"origin": "http://localhost:5173"}}
    auth = {"Authorization": "Bearer user-a"}
    site_path = f"/api/data-source-sets/{session.data_source_set_id}/site-context"
    goal = "来店前の案内を見つけやすくする"
    site = {
        "site_basics": "店舗案内の情報サイト",
        "goals": goal,
        "technical_constraints": "店舗担当者が催事情報を更新する",
        "content_policy": "既存案内の見つけやすさを重視",
        "allowed_surfaces": [],
        "allow_new_content": False,
    }
    settings = api.put(site_path, headers=auth, json=site)
    assert settings.status_code == 200, settings.text
    assert (
        api.get(site_path, headers={"Authorization": "Bearer user-b"}).status_code
        == 403
    )
    old_context = api.get(path + "/browser/context", headers=auth).json()
    assert old_context["site_context"]["goals"] == goal
    assert old_context["site_context"]["role"] == "background_not_policy"
    assert api.get("/api/me", headers=auth).json()["browser_agent_available"]
    assert (
        api.post(
            path + "/agent/stream", headers=auth, json={"content": "test"}
        ).status_code
        == 403
    )
    with (
        pytest.raises(WebSocketDisconnect),
        api.websocket_connect(
            path + "/browser/agent", headers={"origin": "https://unapproved.example"}
        ),
    ):
        pass
    with api.websocket_connect(path + "/browser/agent", **options) as socket:
        socket.send_json(
            {
                "kind": "start",
                "token": "user-b",
                "content": "test",
                "operation_id": "denied-001",
            }
        )
        assert socket.receive_json() == {
            "event": "error",
            "data": {"code": "access_denied"},
        }
    with api.websocket_connect(path + "/browser/agent", **options) as socket:
        socket.send_json(
            {
                "kind": "start",
                "token": "user-a",
                "content": "test",
                "operation_id": "wrong-reply-001",
            }
        )
        socket.send_json(
            {
                "kind": "inference_result",
                "request_id": "0" * 32,
                "ok": True,
                "output": "{}",
            }
        )
        frame = socket.receive_json()
        while frame["event"] != "error":
            frame = socket.receive_json()
        assert frame["data"]["code"] == "browser_agent_invalid_frame"
    assert not calls

    def turn(
        question, operation, grouped=False, compare=False, approve=True, repair=None
    ):
        phases = []
        approved_count = 0
        query_count = len(calls)
        with api.websocket_connect(path + "/browser/agent", **options) as socket:
            socket.send_json(
                {
                    "kind": "start",
                    "token": "user-a",
                    "content": question,
                    "operation_id": operation,
                    "approval_protocol": 1,
                }
            )
            while True:
                frame = socket.receive_json()
                if frame["event"] == "error" and not approve:
                    assert frame["data"]["code"] == "browser_plan_declined"
                    return phases, None
                assert frame["event"] != "error", frame
                if frame["event"] == "done":
                    return phases, frame["data"]
                if frame["event"] == "plan_confirmation":
                    phases.append("plan_confirmation")
                    assert len(calls) == query_count + approved_count
                    preview = frame["data"]
                    if preview["retry_reason"]:
                        assert approved_count == 1
                        if repair == "row-limit":
                            assert (
                                preview["retry_reason"]
                                == "mcp_result_row_limit_exceeded"
                            )
                            assert preview["limit"] == 25
                        else:
                            assert (
                                preview["retry_reason"]
                                == "ga4_metric_dimension_incompatible"
                            )
                            assert preview["metrics"] == ["page_views"]
                    if compare:
                        assert preview["period"] == {
                            "start": "2026-06-01",
                            "end": "2026-06-30",
                        }
                        assert preview["comparison_period"] == {
                            "start": "2026-05-01",
                            "end": "2026-05-31",
                        }
                        assert preview["dimensions"] == ["session_source"]
                        assert preview["metrics"] == ["sessions"]
                        assert preview["presentation"] == "table"
                    socket.send_json(
                        {
                            "kind": "plan_confirmation",
                            "request_id": preview["id"],
                            "approved": approve,
                        }
                    )
                    approved_count += 1
                    continue
                if frame["event"] != "inference":
                    continue
                data = frame["data"]
                assert "schema" in data
                phase = data["phase"]
                phases.append(phase)
                context = json.loads(data["context"])
                if phase in {"selection", "ga4_arguments", "advice", "summary"}:
                    assert context["site_context"]["goals"] == goal
                    assert (
                        context["site_context"]["content_direction"]
                        == site["content_policy"]
                    )
                if phase == "selection":
                    wants_query = (
                        len(calls) == query_count or context.get("query_repair")
                    ) and context.get("data_acquisition_allowed", True)
                    payload = {
                        "tool": "ga4.analyze" if wants_query else "evidence.answer",
                        "workflow_id": "analysis.grouped_breakdown.v1"
                        if grouped
                        else "analysis.exploration.v1",
                    }
                elif phase == "ga4_arguments":
                    if "セッションの参照元ごとのPVとUU" in context["user_question"]:
                        assert context["metric_mentions"] == [
                            "page_views",
                            "active_users",
                        ]
                    payload = {
                        "plan": {
                            "intent": "ページと参照元の内訳",
                            "skill_id": "ga4.cross_breakdown.v1",
                            "period": {"start": "2026-05-01", "end": "2026-05-31"},
                            "comparison_period": None,
                            "metric_terms": ["page_views"]
                            if grouped
                            else ["page_views", "active_users"],
                            "dimension_terms": ["page_path", "session_source"],
                        },
                        "observed_filters": [],
                    }
                    if compare:
                        payload["plan"].update(
                            comparison="previous_month",
                            comparison_period={
                                "start": "2026-05-01",
                                "end": "2026-05-31",
                            },
                        )
                    if repair:
                        month = "02" if repair == "row-limit" else "04"
                        payload["plan"]["period"] = {
                            "start": f"2026-{month}-01",
                            "end": f"2026-{month}-" + ("28" if month == "02" else "30"),
                        }
                        if context.get("query_repair"):
                            assert context["query_repair"]["remaining_retries"] == 1
                            if repair == "row-limit":
                                payload["plan"]["ranking"] = {"limit": 25}
                            else:
                                assert context["query_repair"]["code"] == (
                                    "ga4_metric_dimension_incompatible"
                                )
                                # Remove only the auxiliary metric.
                                payload["plan"]["metric_terms"] = ["page_views"]
                            if repair == "invalid":
                                payload["plan"]["period"]["start"] = "2026-03-01"
                    # The staged browser case relies on the user's explicit
                    # parent/child wording, not the model offering a second axis.
                elif phase == "advice":
                    assert context["data_acquisition_allowed"] is False
                    assert "page_views" in context["proposal_metric_terms"]
                    payload = {
                        "proposals": [
                            {
                                "title": "既存の来店案内の見直し",
                                "hypothesis": "案内が目に留まりやすくなる可能性。",
                                "changes": "既存案内の見出しと配置を見直す",
                                "surfaces": ["content"],
                                "success_metric": "page_views",
                                "evidence_ids": context["facts"][0]["evidence_ids"],
                                "goal_basis": "site_context",
                                "goal_quote": goal,
                                "required_evidence": "目的の案内を見つけられたか",
                                "gap": "閲覧数だけでは目的の案内の発見を判断できない",
                                "next_check": "担当者に閲覧動線の確認方法を聞く",
                                "next_check_kind": "human_review",
                            }
                        ]
                    }
                elif phase == "summary":
                    payload = {
                        "explanation": "取得済みの根拠を確認しました。",
                        "assessments": [
                            {
                                "interpretation": "ページ別の内訳を確認しました。",
                                "fact_ids": [f["id"]],
                            }
                            for f in context["facts"][:3]
                        ],
                    }
                else:
                    assert phase in {"summary_grounding", "advice_grounding"}
                    payload = {
                        "source_reasoning": "取得した集計値だけを説明しています。",
                        "measurement_reasoning": "未計測の指標は含めません。",
                        "capability_reasoning": "追加取得を提案していません。",
                        "purpose_reasoning": "今回の依頼は取得した内訳の確認です。",
                        "issues": [],
                        "purpose_coverage": [],
                    }
                socket.send_json(
                    {
                        "kind": "inference_result",
                        "request_id": data["id"],
                        "ok": True,
                        "output": json.dumps(payload),
                    }
                )

    phases, _ = turn(
        "2026年5月のページとセッションの参照元ごとのPVとUUを表にしてください。",
        "browser-cross-001",
    )
    assert phases[:2] == ["selection", "ga4_arguments"]
    assert phases.count("selection") == 1
    assert phases.count("plan_confirmation") == 1
    assert "summary" not in phases  # A table-only request is already satisfied.
    assert len(calls) == 1
    assert calls[0].arguments["dimensions"] == [
        fields["page_path"],
        fields["session_source"],
    ]
    assert calls[0].arguments["metrics"] == [
        fields["page_views"],
        fields["active_users"],
    ]
    staged_question = (
        "2026年5月のページビュー数上位5ページを確定してください。"
        "その5ページそれぞれについて参照元別のPV上位5件を調べてください。"
        "期間の比較は不要です。"
    )
    staged_phases, workspace = turn(staged_question, "browser-rank-001", grouped=True)
    assert "summary" in staged_phases
    assert staged_phases.count("plan_confirmation") == 1
    assert len(calls) == 7  # Prior cross report, ranking and five child reports.
    assert calls[1].arguments["dimensions"] == [fields["page_path"]]
    assert all(
        call.arguments["dimensions"] == [fields["session_source"]] for call in calls[2:]
    )
    assert workspace["staged_plan"]["phase"] == "complete"
    replayed, _ = turn(staged_question, "browser-rank-001", grouped=True)
    assert not replayed and len(calls) == 7
    followup, result = turn(
        "新しいデータ取得はせず、前回のページ別の主要参照元を説明してください。",
        "browser-evidence-001",
    )
    assert "ga4_arguments" not in followup
    assert result["completed_queries"] == 0 and len(calls) == 7
    assert not result["report_message_id"]
    advice_phases, coached = turn(
        "新しいデータ取得はせず、サイト設定と保存済みの結果を踏まえて改善案を提案してください。",
        "browser-advice-001",
    )
    assert advice_phases[:2] == ["advice", "advice_grounding"]
    assert "ga4_arguments" not in advice_phases
    assert coached["status"] == "complete"
    assert coached["proposals_site_revision"] == settings.json()["revision"]
    assert coached["proposals"][0]["evaluation"]["goal_checks"][0]["goal_quote"] == goal
    assert not coached["report_message_id"] and len(calls) == 7

    report_phases, report = turn(
        "追加取得は不要です。ここまでの結果をレポートにまとめてください。",
        "browser-report-001",
    )
    assert not report_phases
    assert report["status"] == "complete"
    saved_id = report["report_message_id"]
    assert saved_id and len(calls) == 7
    _, kept_report = turn(
        "新しいデータ取得はせず、結果を説明してください。レポートは作成しないでください。",
        "browser-no-report-001",
    )
    assert not kept_report["report_requested"]
    assert kept_report["report_message_id"] == saved_id
    assert len(calls) == 7

    history = api.get(path + "/history", headers=auth)
    assert history.status_code == 200
    assert len(history.json()["results"]) >= 7
    saved = next(m for m in history.json()["messages"] if m["id"] == saved_id)
    assert saved["content_format"] == "markdown"
    assert saved["content"].startswith("# 分析レポート")
    assert saved["evidence_ids"] and "## レポートの範囲" in saved["content"]
    question = (
        "2026年5月と6月のセッションの参照元を、セッション数で比較してください。"
        "6月を対象、5月を比較基準にして、参照元ごとの差を表で出してください。"
    )
    direct = explicit_comparison_table(question, definitions)
    assert direct is not None
    assert direct.metric_terms == ("sessions",)
    assert direct.dimension_terms == ("session_source",)
    assert direct.time_grain is None
    for suffix in (
        "Organic Searchだけに絞ってください。",
        "日別でも集計してください。",
        "前回のグラフから再取得なしで。",
        "原因と改善案も説明してください。",
        "上位10件にしてください。",
    ):
        assert explicit_comparison_table(question + suffix, definitions) is None
    assert explicit_comparison_table(question, []) is None
    other = explicit_comparison_table(
        "2026年6月と2026年5月のページ別のPVを比較して、"
        "5月を対象、6月を比較基準にして、表で表示してください。",
        definitions,
    )
    assert other is not None
    assert other.metric_terms == ("page_views",)
    assert other.dimension_terms == ("page_path",)
    assert other.period and other.period.start.month == 5
    assert other.comparison_period and other.comparison_period.start.month == 6
    canceled, _ = turn(question, "browser-compare-no", compare=True, approve=False)
    assert canceled == ["plan_confirmation"] and len(calls) == 7
    checked, workspace = turn(question, "browser-compare-yes", compare=True)
    assert checked == ["plan_confirmation"]
    assert workspace["status"] == "complete"
    assert len(calls) == 8
    assert calls[-1].arguments["dimensions"] == ["sessionSource"]
    assert calls[-1].arguments["metrics"] == ["sessions"]
    saved_history = api.get(path + "/history", headers=auth).json()
    findings = saved_history["latest_result"]["findings"]
    matched = next(
        r for r in findings[0]["category_comparisons"] if r["status"] == "matched"
    )
    assert matched["difference"] == "20"
    single = next(
        r for r in findings[0]["category_comparisons"] if r["status"] == "current_only"
    )
    assert single["comparison_value"] is None and single["difference"] is None

    april = "2026年4月のPVを調べて、結果を説明してください。"
    faults.append("ga4_metric_dimension_incompatible")
    repaired, outcome = turn(april, "browser-repair-ok", repair="valid")
    assert repaired.count("plan_confirmation") == 2
    assert outcome["status"] == "complete"
    assert len(calls) == 10
    assert calls[-1].arguments["metrics"] == ["screenPageViews"]
    assert calls[-1].arguments["date_ranges"] == calls[-2].arguments["date_ranges"]

    faults.append("ga4_metric_dimension_incompatible")
    blocked, outcome = turn(april, "browser-repair-no", repair="invalid")
    assert blocked.count("plan_confirmation") == 1
    assert outcome["status"] == "partial"
    assert len(calls) == 11

    faults.append("mcp_result_shape_invalid")
    terminal, outcome = turn(april, "browser-repair-stop", repair="valid")
    assert terminal.count("ga4_arguments") == 1
    assert terminal.count("plan_confirmation") == 1
    assert outcome["status"] == "partial"
    assert len(calls) == 12

    faults.append("mcp_result_row_limit_exceeded")
    smaller, outcome = turn(
        "2026年2月のPVを取得して説明してください。",
        "browser-repair-row-limit",
        repair="row-limit",
    )
    assert smaller.count("plan_confirmation") == 2
    assert outcome["status"] == "complete"
    assert len(calls) == 14
    assert calls[-1].arguments["limit"] == 25 < calls[-2].arguments["limit"]
