import hashlib
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from analytics_ai.agent.models import AgentDecision, ReportSection
from analytics_ai.agent.service import AgentService
from analytics_ai.agent.staged_history import recover_staged
from analytics_ai.agent.staged_models import GroupedRequest
from analytics_ai.agent.store import MemoryAgentStore, record_id
from analytics_ai.api.app import create_app
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    AnalysisMessage,
    AnalysisResultReference,
    DatePeriod,
    MessageRole,
    Status,
)
from analytics_ai.mcp_gateway.executor import ToolExecutionResult
from analytics_ai.orchestrator.planning import IntentPlan
from tests.test_agent import Director
from tests.test_api_authorization import StaticVerifier, auth
from tests.test_cross_breakdown import register
from tests.test_director_continuation import query, selection, vertex
from tests.test_orchestrator import add_session, build_orchestrator

QUESTION = (
    "2026年5月のPV上位5ページを最初に確定してください。"
    "その各ページの参照元別PV上位5件を調べてください。"
    "期間比較は不要です。レポートはまだ作成しないでください。"
)
EXPLAIN = (
    "新しいデータ取得はせず、前回までに取得した5ページの分析結果を使って説明してください。"
    "最初に伝えた分析目的と制約を確認し、ページごとの参照元の違いと改善仮説を整理してください。"
    "根拠となるページ・参照元・数値を示し、PVだけでは判断できないことも明記してください。"
    "レポートはまだ作成しないでください。"
)


@pytest.fixture
async def saved_group(repository):
    original = await add_session(repository)
    fields = register(repository)
    plan = IntentPlan(
        intent="上位ページ",
        metric_terms=("page_views",),
        period=DatePeriod(start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    director = Director(
        [
            AgentDecision(
                action="query",
                objective="広告予算を増やさず既存ページを改善",
                explanation="対象を確認します。",
                plan=plan,
                grouped_request=GroupedRequest(
                    primary="page_path", secondary="session_source", limit=5
                ),
            )
        ]
    )
    orchestrator, executor = build_orchestrator(repository, [], {"rows": []})
    calls = []

    async def execute(invocation):
        calls.append(invocation)
        rows = (
            [
                {fields["page_path"]: f"/page-{i}/", "screenPageViews": str(100 - i)}
                for i in range(5)
            ]
            if invocation.arguments["dimensions"] == [fields["page_path"]]
            else [
                {
                    fields["session_source"]: f"source-{i}",
                    "screenPageViews": str(15 - i),
                }
                for i in range(5)
            ]
        )
        return ToolExecutionResult(
            payload={"rows": rows},
            query_id=None,
            bytes_processed=None,
            row_count=len(rows),
        )

    executor.execute = execute
    store = MemoryAgentStore()
    agent = AgentService(repository, store, director, orchestrator)
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=original.id,
            content=QUESTION,
            operation_id="seed-group",
        )
    ]
    assert events[-1]["event"] == "done" and events[-1]["data"]["status"] == "complete"
    assert len(calls) == 6

    assert all(
        e["data"]["visualization"]["type"] == "bar"
        for e in events
        if e["event"] == "analysis"
    )
    target = original.model_copy(update={"id": "as_new_conversation"})
    await repository.create_session(target)
    source = await agent.session("user-a", original.id)
    workspace = await agent.workspace("user-a", original.id)
    cited = tuple(e for s in workspace.sections for e in s.evidence_ids)
    old_answer = AnalysisMessage(
        id="msg_" + "a" * 32,
        request_id="prior-answer",
        session_id=target.id,
        user_id=target.user_id,
        company_id=target.company_id,
        data_source_set_id=target.data_source_set_id,
        role=MessageRole.ASSISTANT,
        content="以前の取得結果を引用しました。",
        evidence_ids=cited,
    )
    await repository.create_message(old_answer)
    return repository, agent, orchestrator, calls, source, target, cited


async def competing_history(saved_group):
    repository, agent, _, _, source, target, _ = saved_group
    workspace = await agent.workspace(source.user_id, source.id)
    section = workspace.sections[1]
    original_finding = await repository.get_finding(section.finding_ids[0])
    original_evidence = await repository.get_evidence(section.evidence_ids[0])
    for index in range(9):
        competitor = source.model_copy(update={"id": f"as_competitor_{index}"})
        await repository.create_session(competitor)
        eid = "evidence_" + uuid.uuid4().hex
        fid = "finding_" + uuid.uuid4().hex
        await repository.create_evidence(
            original_evidence.model_copy(update={"id": eid})
        )
        await repository.create_finding(
            original_finding.model_copy(
                update={"id": fid, "session_id": competitor.id, "evidence_ids": (eid,)}
            )
        )
        await agent.memory.archive(
            competitor,
            EXPLAIN + QUESTION,
            section.model_copy(
                update={
                    "message_id": "msg_" + uuid.uuid4().hex,
                    "finding_ids": (fid,),
                    "evidence_ids": (eid,),
                }
            ),
        )
    hits = (
        await agent.memory.search(source.user_id, target.data_source_set_id, EXPLAIN)
    )["hits"]
    assert len(hits) == 8
    assert all(hit["session_id"] != source.id for hit in hits)
    return workspace, hits


@pytest.mark.asyncio
@pytest.mark.parametrize("modern_answer", [False, True])
async def test_cited_plan_survives_eight_higher_ranked_reports(
    saved_group, modern_answer
):
    repository, agent, orchestrator, calls, source, target, cited = saved_group
    workspace, hits = await competing_history(saved_group)
    fids = tuple(fid for section in workspace.sections for fid in section.finding_ids)
    if modern_answer:
        previous = (await repository.list_messages(target.id))[-1]
        repository.messages[previous.id] = previous.model_copy(
            update={"finding_ids": fids}
        )
        # Direct lineage works even when the cited source is outside the
        # archive window. Its authorized workspace still holds all six steps.
        agent.store.records = {
            key: record
            for key, record in agent.store.records.items()
            if not (
                record.kind == "episode"
                and record.payload.get("session_id") == source.id
            )
        }
    agent.director = Director([])
    events = [
        e
        async for e in agent.turn(
            user_id=target.user_id,
            session_id=target.id,
            content=EXPLAIN,
            operation_id="crowded-history",
        )
    ]
    assert events[-1]["data"]["status"] == "complete", events[-1]
    assert len(calls) == 6  # Fixture only: the explanation must never acquire data.
    context = agent.director.contexts[-1]
    assert {f["id"] for f in context["facts"]} == set(fids)
    assert not context["missing_requested_fields"]
    messages = await repository.list_messages(target.id)
    answer = messages[-1]
    assert set(answer.evidence_ids) == set(cited)
    assert len(answer.result_references) == 6
    assert all(ref.session_id == source.id for ref in answer.result_references)
    assert not events[-1]["data"]["report_requested"]
    assert "未確認の指定条件" not in answer.content
    restored = await orchestrator.restore_results(session=target, messages=messages)
    assert len(restored) == 6
    assert all(r.parent_message_id == answer.id for r in restored)
    assert all(r.source_session_id == source.id for r in restored)
    assert len({r.visualization.title for r in restored}) == 6
    assert all(hit["session_id"] != source.id for hit in hits)


@pytest.mark.asyncio
async def test_direct_findings_recover_without_ranked_or_archived_hits(
    saved_group, monkeypatch
):
    repository, agent, _, calls, source, target, cited = saved_group
    workspace = await agent.workspace(source.user_id, source.id)
    fids = tuple(fid for section in workspace.sections for fid in section.finding_ids)
    reads = []

    async def unavailable_archive(kind, owner, source, limit):
        reads.append((kind, owner, source, limit))
        return [], True  # The source's workspace is intact beyond the scan window.

    monkeypatch.setattr(agent.store, "list_scope", unavailable_archive)
    recovered = await recover_staged(
        repository,
        agent.store,
        target,
        [],
        preferred_evidence_ids=cited,
        preferred_finding_ids=fids,
    )
    assert recovered and len(recovered.hits) == 6
    assert recovered.source_session.id == source.id
    assert reads == [("episode", source.user_id, source.data_source_set_id, 500)]
    assert len(calls) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "owner",
        "company",
        "source_set",
        "semantic",
        "uncited_evidence",
        "source_disabled",
        "evidence_missing",
        "grant_revoked",
        "filter",
    ],
)
async def test_direct_citations_reject_invalid_lineage_without_search_fallback(
    saved_group, invalid
):
    repository, agent, _, calls, source, target, cited = saved_group
    workspace = await agent.workspace(source.user_id, source.id)
    fids = tuple(fid for section in workspace.sections for fid in section.finding_ids)
    finding = repository.findings[fids[0]]
    fields = {
        "owner": "user_id",
        "company": "company_id",
        "source_set": "data_source_set_id",
        "semantic": "semantic_version",
    }
    if invalid in fields:
        repository.findings[finding.id] = finding.model_copy(
            update={fields[invalid]: "other"}
        )
    elif invalid == "missing":
        repository.findings.pop(finding.id)
    elif invalid == "uncited_evidence":
        repository.findings[finding.id] = finding.model_copy(
            update={"evidence_ids": ("other",)}
        )
    elif invalid == "source_disabled":
        repository.sessions[source.id] = source.model_copy(
            update={"status": Status.DISABLED}
        )
    elif invalid == "evidence_missing":
        repository.evidence.pop(cited[-1])
    elif invalid == "grant_revoked":
        repository.grants.clear()
    kwargs = {}
    if invalid == "filter":
        from analytics_ai.domain.models import AnalysisFilter

        kwargs["required_filters"] = (
            AnalysisFilter(
                semantic_name="mobile",
                kind="device",
                field_name="deviceCategory",
                operator="exact",
                value="mobile",
            ),
        )
    if invalid == "grant_revoked":
        with pytest.raises(AccessDenied):
            await recover_staged(
                repository,
                agent.store,
                target,
                [],
                preferred_evidence_ids=cited,
                preferred_finding_ids=fids,
            )
    else:
        assert (
            await recover_staged(
                repository,
                agent.store,
                target,
                [{"session_id": source.id}],
                preferred_evidence_ids=cited,
                preferred_finding_ids=fids,
                **kwargs,
            )
            is None
        )
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_missing_cited_source_does_not_substitute_competing_reports(saved_group):
    repository, agent, _, calls, source, target, _cited = saved_group
    workspace, _ = await competing_history(saved_group)
    previous = (await repository.list_messages(target.id))[-1]
    fids = tuple(fid for section in workspace.sections for fid in section.finding_ids)
    repository.messages[previous.id] = previous.model_copy(update={"finding_ids": fids})
    repository.sessions[source.id] = source.model_copy(
        update={"status": Status.DISABLED}
    )
    agent.director = Director([])
    events = [
        e
        async for e in agent.turn(
            user_id=target.user_id,
            session_id=target.id,
            content=EXPLAIN,
            operation_id="missing-cited-source",
        )
    ]
    assert events[-1]["data"]["status"] != "complete"
    assert not agent.director.contexts[-1]["facts"]
    answer = (await repository.list_messages(target.id))[-1]
    assert not answer.evidence_ids and not answer.result_references
    assert len(calls) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["owner", "source_set", "fingerprint", "semantic", "record_id"]
)
async def test_legacy_exact_match_rejects_corrupt_archive_scope(saved_group, invalid):
    repository, agent, _, calls, source, target, cited = saved_group
    for key, record in list(agent.store.records.items()):
        if record.kind != "episode":
            continue
        if invalid == "owner":
            record = record.model_copy(update={"owner_id": "other"})
        elif invalid == "source_set":
            record = record.model_copy(update={"data_source_set_id": "other"})
        elif invalid == "record_id":
            record = record.model_copy(update={"id": "other"})
        else:
            field = (
                "source_fingerprint" if invalid == "fingerprint" else "semantic_version"
            )
            record = record.model_copy(
                update={"payload": {**record.payload, field: "other"}}
            )
        agent.store.records[key] = record
    assert (
        await recover_staged(
            repository,
            agent.store,
            target,
            [{"session_id": source.id}],
            preferred_evidence_ids=cited,
        )
        is None
    )
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_legacy_citations_reject_ambiguous_plans_and_scan_only_once(
    saved_group, monkeypatch
):
    repository, agent, _, calls, source, target, cited = saved_group
    record = await agent.store.get("workspace", record_id(source.user_id, source.id))
    other = source.model_copy(update={"id": "as_ambiguous"})
    await repository.create_session(other)
    await agent.store.save(
        record.model_copy(
            update={
                "id": record_id(other.user_id, other.id),
                "revision": 1,
            }
        ),
        0,
    )
    section = ReportSection.model_validate(record.payload["sections"][0])
    await agent.memory.archive(other, QUESTION, section)
    scan = agent.store.list_scope
    counts = []

    async def counted(*args):
        counts.append(args)
        return await scan(*args)

    monkeypatch.setattr(agent.store, "list_scope", counted)
    assert (
        await recover_staged(
            repository, agent.store, target, [], preferred_evidence_ids=cited
        )
        is None
    )
    assert len(counts) == 1 and counts[0][-1] == 500
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_citation_budget_fails_closed_before_reads(saved_group, monkeypatch):
    repository, agent, _, _, _, target, cited = saved_group

    async def forbidden(*args):
        raise AssertionError("Unbounded citation lookup")

    monkeypatch.setattr(repository, "get_finding", forbidden)
    monkeypatch.setattr(agent.store, "list_scope", forbidden)
    assert (
        await recover_staged(
            repository,
            agent.store,
            target,
            [],
            preferred_evidence_ids=cited,
            preferred_finding_ids=tuple(f"finding-{i}" for i in range(33)),
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_fetch_recovers_cited_plan_and_all_six_visualizations(saved_group):
    repository, agent, orchestrator, calls, source, target, _cited = saved_group
    agent.director = Director([])  # no planning or acquisition permitted
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=target.id,
            content=EXPLAIN,
            operation_id="explain-history",
        )
    ]
    assert events[-1]["event"] == "done", events[-1]
    assert events[-1]["data"]["status"] == "complete", events[-1]
    assert len(calls) == 6
    context = agent.director.contexts[-1]
    assert len(context["facts"]) == 6
    assert not context["missing_requested_fields"]
    assert context["data_acquisition_allowed"] is False
    messages = await repository.list_messages(target.id)
    answer = messages[-1]
    assert len(answer.result_references) == 6
    assert "未確認の指定条件" not in answer.content
    assert (
        "広告予算" in context["staged_analysis"]["objective"]
        or context["staged_analysis"]["objective"] == QUESTION
    )
    for i in range(5):
        assert f"/page-{i}/" in answer.content
    # Reloading history does not need GA4 or a report artifact.
    results = await orchestrator.restore_results(session=target, messages=messages)
    assert len(results) == 6
    assert {r.parent_message_id for r in results} == {answer.id}
    assert {r.source_session_id for r in results} == {source.id}
    assert len({r.message_id for r in results}) == 6
    assert all(r.rows for r in results)
    assert not events[-1]["data"]["report_requested"]
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_new_grouped_request_with_history_materializes_cached_results(
    saved_group,
):
    repository, agent, orchestrator, calls, _source, target, _cited = saved_group
    director, model_calls = vertex(
        [
            selection("evidence.answer"),
            query(
                metric_terms=["page_views"],
                period={"start": "2026-05-01", "end": "2026-05-31"},
            ),
        ]
    )
    director.summarize = Director([]).summarize
    agent.director = director
    events = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=target.id,
            content=QUESTION,
            operation_id="new-group-history",
        )
    ]
    assert events[-1]["event"] == "done", events[-1]
    assert events[-1]["data"]["status"] == "complete", events[-1]
    assert len(model_calls) == 2
    assert len(calls) == 6  # six exact cache hits, no duplicate external fetch
    assert len([e for e in events if e["event"] == "analysis"]) == 6
    assert all(e["data"]["cache_hit"] for e in events if e["event"] == "analysis")
    results = await orchestrator.restore_results(
        session=await agent.session("user-a", target.id),
        messages=await repository.list_messages(target.id),
    )
    assert len(results) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    ["uncited", "period", "metric", "evidence", "source_owner", "source_inactive"],
)
async def test_recovery_rejects_unverified_or_changed_scope(saved_group, invalid):
    repository, agent, _orchestrator, calls, source, target, cited = saved_group
    hits = (await agent.memory.search("user-a", target.data_source_set_id, QUESTION))[
        "hits"
    ]
    kwargs = {"preferred_evidence_ids": cited}
    if invalid == "uncited":
        kwargs["preferred_evidence_ids"] = ()
    if invalid == "period":
        kwargs["required_period"] = DatePeriod(
            start=date(2026, 6, 1), end=date(2026, 6, 30)
        )
    if invalid == "metric":
        kwargs["required_metric_fields"] = ("activeUsers",)
    if invalid == "evidence":
        repository.evidence.pop(cited[-1])
    if invalid == "source_owner":
        repository.sessions[source.id] = source.model_copy(
            update={"user_id": "other-user"}
        )
    if invalid == "source_inactive":
        from analytics_ai.domain.models import Status

        repository.sessions[source.id] = source.model_copy(
            update={"status": Status.DISABLED}
        )
    assert await recover_staged(repository, agent.store, target, hits, **kwargs) is None
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_attached_history_revocation_and_recursive_references_are_not_exposed(
    saved_group,
):
    repository, agent, orchestrator, _calls, source, target, _cited = saved_group
    direct = next(
        m for m in await repository.list_messages(source.id) if m.visualization
    )
    scope = await agent.authorization.authorize_session("user-a", source)
    parent = AnalysisMessage(
        id="msg_" + "b" * 32,
        request_id="attached",
        session_id=target.id,
        user_id=target.user_id,
        company_id=target.company_id,
        data_source_set_id=target.data_source_set_id,
        role=MessageRole.ASSISTANT,
        content="参照結果",
        result_references=(
            AnalysisResultReference(
                session_id=source.id,
                message_id=direct.id,
                source_fingerprint=hashlib.sha256(
                    str(scope.source_set.ga4).encode()
                ).hexdigest(),
                semantic_version=source.state.semantic_version,
            ),
        ),
    )
    assert (
        len(await orchestrator.restore_results(session=target, messages=[parent])) == 1
    )
    repository.sessions[source.id] = source.model_copy(update={"user_id": "other-user"})
    assert not await orchestrator.restore_results(session=target, messages=[parent])
    repository.sessions[source.id] = source
    recursive = parent.model_copy(
        update={
            "result_references": (
                AnalysisResultReference(session_id=target.id, message_id=parent.id),
            )
        }
    )
    assert not await orchestrator.restore_results(session=target, messages=[recursive])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["scope_fingerprint", "semantic_version", "cache_blob", "evidence"]
)
async def test_attached_history_rejects_changed_scope_or_missing_source(
    saved_group, invalid
):
    repository, agent, orchestrator, calls, _source, target, _cited = saved_group
    agent.director = Director([])
    _ = [
        e
        async for e in agent.turn(
            user_id="user-a",
            session_id=target.id,
            content=EXPLAIN,
            operation_id="attached-guard",
        )
    ]
    messages = await repository.list_messages(target.id)
    answer = messages[-1]
    assert (
        len(await orchestrator.restore_results(session=target, messages=messages)) == 6
    )
    if invalid in {"scope_fingerprint", "semantic_version"}:
        key = (
            "source_fingerprint"
            if invalid == "scope_fingerprint"
            else "semantic_version"
        )
        answer = answer.model_copy(
            update={
                "result_references": tuple(
                    r.model_copy(update={key: "changed"})
                    for r in answer.result_references
                )
            }
        )
    elif invalid == "cache_blob":
        orchestrator._cache._blobs.items.clear()
    else:
        repository.evidence.clear()
    assert not await orchestrator.restore_results(session=target, messages=[answer])
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_corrupt_chart_does_not_hide_saved_messages_or_other_charts(saved_group):
    repository, _agent, orchestrator, calls, source, _target, _cited = saved_group
    blobs = orchestrator._cache._blobs.items
    blobs[next(iter(blobs))] = b"[{}]"
    api = TestClient(
        create_app(
            Settings(environment="test"),
            repository=repository,
            token_verifier=StaticVerifier(),
            orchestrator=orchestrator,
        )
    )
    response = api.get(f"/api/sessions/{source.id}/history", headers=auth("user-a"))
    assert response.status_code == 200
    assert response.json()["messages"]
    assert len(response.json()["results"]) == 5
    assert len(calls) == 6
