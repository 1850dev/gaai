"""Deterministic working memory and authorized, bounded historical retrieval.

No embeddings, new cloud service, credential fallback or model-owned writes.
Stored prose is untrusted context; measured facts are loaded from Evidence.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from analytics_ai.agent.facts import finding_context
from analytics_ai.agent.memory_models import (
    Agreement,
    AgreementChange,
    MemoryEntry,
    WorkingMemory,
)
from analytics_ai.agent.models import AgentRecord, ReportSection
from analytics_ai.agent.store import AgentStore, record_id
from analytics_ai.authorization.service import AccessDenied, AuthorizationService
from analytics_ai.domain.models import AnalysisSession, AuditEvent, utc_now
from analytics_ai.orchestrator.service import AnalysisOrchestrationError
from analytics_ai.repositories.protocols import ApplicationRepository
from analytics_ai.visualization.planner import DIMENSION_LABELS, METRIC_LABELS


def remember_user(
    memory: WorkingMemory, content: str, message_id: str
) -> WorkingMemory:
    additions: list[MemoryEntry] = []
    for sentence in re.split(r"[。\n]", content):
        sentence = sentence.strip()
        if not sentence:
            continue
        kind = (
            "decision"
            if re.search(r"方針|禁止|不可|対象外|しない|決定|合意", sentence)
            else "objective"
            if re.search(r"目的|目標|ゴール", sentence)
            else "question"
            if sentence.endswith(("?", "？"))
            else None
        )
        if kind:
            additions.append(
                MemoryEntry(text=sentence[:1000], message_id=message_id, kind=kind)
            )
    if not any(e.kind == "objective" for e in memory.entries):
        additions.insert(
            0, MemoryEntry(text=content[:1000], message_id=message_id, kind="objective")
        )
    for path in re.findall(r"(?:https?://[^\s/]+)?/[^\s、。『』「」<>]+", content):
        if len(path) <= 300:
            additions.append(
                MemoryEntry(text=path, message_id=message_id, kind="target")
            )
    entries = list(memory.entries)
    for entry in additions:
        entries = [e for e in entries if (e.kind, e.text) != (entry.kind, entry.text)]
        entries.append(entry)
    return memory.model_copy(
        update={
            "entries": tuple(entries[-40:]),
            "trimmed": memory.trimmed or len(entries) > 40,
        }
    )


def search_terms(text: str) -> set[str]:
    normalized = text.casefold()
    aliases = {
        "pv": "screenpageviews",
        "page_views": "screenpageviews",
        "uu": "activeusers",
        "active_users": "activeusers",
        "参照元": "sessionsource",
        "流入元": "sessionsource",
        "ページ": "pagepath",
        "端末": "devicecategory",
    }
    aliases.update(
        {
            label.casefold(): field.casefold()
            for field, label in {**DIMENSION_LABELS, **METRIC_LABELS}.items()
        }
    )
    expanded = {field for alias, field in aliases.items() if alias in normalized}
    months = {
        f"month:{int(y):04d}-{int(m):02d}"
        for y, m in re.findall(
            r"(20\d{2})(?:年\s*|-)(\d{1,2})(?:月|(?=-\d{2}))", normalized
        )
    }
    words = set(
        re.findall(r"[a-z0-9_/-]{2,}|[\u3040-\u30ff\u3400-\u9fff]{2,}", normalized)
    )
    return (
        expanded
        | months
        | words
        | {w[i : i + 2] for w in words if not w.isascii() for i in range(len(w) - 1)}
    )


class ConversationMemory:
    def __init__(self, repository: ApplicationRepository, store: AgentStore) -> None:
        self.repository, self.store = repository, store
        self.authorization = AuthorizationService(repository)

    @staticmethod
    def check_record(record: AgentRecord, user: str, source: str) -> None:
        if record.owner_id != user or record.data_source_set_id != source:
            raise AccessDenied("memory_scope_mismatch")

    async def agreements(self, user: str, source: str) -> list[Agreement]:
        await self.authorization.authorize_source_set(user, source)
        record = await self.store.get("agreements", record_id(user, source))
        if not record:
            return []
        self.check_record(record, user, source)
        return [
            Agreement.model_validate(a)
            for a in record.payload.get("items", [])
            if a["status"] != "deleted"
        ]

    async def change_agreement(
        self,
        user: str,
        source: str,
        change: AgreementChange,
        *,
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> Agreement:
        await self.authorization.authorize_source_set(user, source)
        key = record_id(user, source)
        record = await self.store.get("agreements", key)
        if record:
            self.check_record(record, user, source)
        items = (
            [Agreement.model_validate(a) for a in record.payload.get("items", [])]
            if record
            else []
        )
        if change.action == "propose":
            # A replay of an explicit user request does not duplicate pending memory.
            same = next(
                (
                    a
                    for a in items
                    if a.status != "deleted" and a.text == change.text.strip()
                ),
                None,
            )
            if same:
                return same
            if len(items) >= 100:
                raise AnalysisOrchestrationError("agreement_limit_reached")
            updated = Agreement(
                id=uuid.uuid4().hex,
                revision=1,
                text=change.text.strip(),
                source_session_id=session_id,
                source_message_id=message_id,
            )
        else:
            current = next((a for a in items if a.id == change.id), None)
            if not current or current.status == "deleted":
                raise AccessDenied("agreement_not_found")
            if current.revision != change.revision:
                raise AnalysisOrchestrationError("agent_revision_conflict")
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                    "status": "confirmed"
                    if change.action == "confirm"
                    else "deleted"
                    if change.action == "delete"
                    else "pending",
                    "text": ""
                    if change.action == "delete"
                    else change.text.strip()
                    if change.action == "edit"
                    else current.text,
                    "source_session_id": None
                    if change.action == "delete"
                    else current.source_session_id,
                    "source_message_id": None
                    if change.action == "delete"
                    else current.source_message_id,
                }
            )
        # Do not log text. Attempt audit is durable even if the CAS loses a race.
        await self.repository.write_audit_event(
            AuditEvent(
                id="audit_" + uuid.uuid4().hex,
                request_id="memory",
                user_id=user,
                data_source_set_id=source,
                action="memory.agreement." + change.action,
                status="attempt",
                details={"agreement_id": updated.id, "revision": updated.revision},
            )
        )
        items = [updated if a.id == updated.id else a for a in items]
        if not any(a.id == updated.id for a in items):
            items.append(updated)
        revision = record.revision if record else 0
        if not await self.store.save(
            AgentRecord(
                id=key,
                kind="agreements",
                owner_id=user,
                data_source_set_id=source,
                revision=revision + 1,
                payload={"items": [a.model_dump(mode="json") for a in items]},
            ),
            revision,
        ):
            raise AnalysisOrchestrationError("agent_revision_conflict")
        return updated

    async def archive(
        self, session: AnalysisSession, question: str, section: ReportSection
    ) -> None:
        scope = await self.authorization.authorize_session(session.user_id, session)
        key = record_id(session.user_id, session.id, section.message_id)
        old = await self.store.get("episode", key)
        if old:
            self.check_record(old, session.user_id, session.data_source_set_id)
            return
        await self.store.save(
            AgentRecord(
                id=key,
                kind="episode",
                owner_id=session.user_id,
                data_source_set_id=session.data_source_set_id,
                revision=1,
                payload={
                    "session_id": session.id,
                    "question": question[:1000],
                    "semantic_version": session.state.semantic_version,
                    "source_fingerprint": record_id(
                        scope.source_set.company_id, str(scope.source_set.ga4)
                    ),
                    "recorded_at": utc_now().isoformat(),
                    "section": section.model_dump(mode="json", exclude={"facts"}),
                },
            ),
            0,
        )

    async def search(
        self, user: str, source: str, question: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        scope = await self.authorization.authorize_source_set(user, source)
        fingerprint = record_id(scope.source_set.company_id, str(scope.source_set.ga4))
        records, truncated = await self.store.list_scope("episode", user, source, 500)
        terms = search_terms(question)
        ranked = []
        for record in records:
            self.check_record(record, user, source)
            payload = record.payload
            if payload.get("source_fingerprint") != fingerprint:
                continue
            section = ReportSection.model_validate(payload["section"])
            haystack = " ".join(
                (
                    payload["question"],
                    section.title,
                    *section.metric_fields,
                    *section.dimension_fields,
                    str(section.period),
                    str(section.comparison_period),
                    *(
                        str(row.get(d, ""))
                        for row in section.observations
                        for d in section.dimension_fields
                    ),
                )
            )
            candidates = search_terms(haystack)
            requested_months = {term for term in terms if term.startswith("month:")}
            # Explicit months are stronger than generic page/metric overlap.
            score = len(terms & candidates) + 20 * len(requested_months & candidates)
            if score or payload["session_id"] == session_id:
                ranked.append((score, payload.get("recorded_at", ""), record))
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        hits = []
        for _, _, record in ranked[:32]:
            payload = record.payload
            session = await self.repository.get_session(payload["session_id"])
            if (
                not session
                or session.user_id != user
                or session.data_source_set_id != source
            ):
                continue
            try:
                await self.authorization.authorize_session(user, session)
            except AccessDenied:
                continue
            section = ReportSection.model_validate(payload["section"])
            # Payload text is not accepted as a fact. Every citation is reloaded.
            valid = []
            fact_list = []
            for fid in section.finding_ids:
                finding = await self.repository.get_finding(fid)
                if not finding or (
                    finding.user_id,
                    finding.session_id,
                    finding.data_source_set_id,
                ) != (user, session.id, source):
                    continue
                evidence = [
                    await self.repository.get_evidence(eid)
                    for eid in finding.evidence_ids
                ]
                if evidence and all(
                    e
                    and e.data_source_set_id == source
                    and e.company_id == session.company_id
                    and e.dataset_id == finding.dataset_id
                    and (
                        not scope.source_set.ga4
                        or e.ga4_property_id == scope.source_set.ga4.property_id
                    )
                    for e in evidence
                ):
                    valid.append(fid)
                    fact_list.append(
                        {
                            **finding_context(finding, list(finding.evidence_ids)),
                            "period": section.period.model_dump(mode="json")
                            if section.period
                            else None,
                            "comparison_period": section.comparison_period.model_dump(
                                mode="json"
                            )
                            if section.comparison_period
                            else None,
                            "filters": [
                                f.model_dump(mode="json") for f in section.filters
                            ],
                            "historical": True,
                            "recorded_at": payload["recorded_at"],
                            "source_session_id": session.id,
                        }
                    )
            if not valid or set(valid) != set(section.finding_ids):
                continue
            hits.append(
                {
                    "session_id": session.id,
                    "question": payload["question"],
                    "recorded_at": payload["recorded_at"],
                    "semantic_version": payload.get("semantic_version"),
                    "historical": True,
                    "facts": fact_list,
                    "section": section.model_dump(mode="json"),
                }
            )
            if len(hits) >= 8:
                break
        latest = await self.authorization.authorize_source_set(user, source)
        if (
            record_id(latest.source_set.company_id, str(latest.source_set.ga4))
            != fingerprint
        ):
            raise AccessDenied("memory_source_changed")
        return {
            "hits": hits,
            "candidate_limit": 500,
            "truncated": truncated or (len(ranked) > 32 and len(hits) < 8),
        }
