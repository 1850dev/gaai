"""Durable per-request ledger. Unknown external outcomes never trigger a retry."""

import hashlib
import json
from datetime import datetime
from typing import Literal

from analytics_ai.agent.models import AgentRecord, AgentWorkspace
from analytics_ai.agent.store import AgentStore, record_id
from analytics_ai.domain.models import FrozenModel
from analytics_ai.orchestrator.service import AnalysisOrchestrationError


def request_hash(content: str, background: bool, period: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"content": content, "background": background, "period": period},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


class Operation(FrozenModel):
    session_id: str
    operation_id: str
    request_hash: str
    source_hash: str
    semantic_version: str
    status: Literal["running", "prepared", "complete", "failed", "interrupted"]
    lease_until: datetime | None
    workspace_json: str | None = None
    error_code: str | None = None


class OperationLedger:
    def __init__(self, store: AgentStore):
        self.store = store

    async def get(self, user: str, session: str, operation: str) -> AgentRecord | None:
        record = await self.store.get("operation", record_id(user, session, operation))
        if record and (
            record.owner_id != user
            or Operation.model_validate(record.payload).session_id != session
        ):
            raise AnalysisOrchestrationError("operation_scope_mismatch")
        return record

    async def save(
        self, user: str, source: str, value: Operation, previous: AgentRecord | None
    ) -> AgentRecord:
        revision = previous.revision if previous else 0
        if previous:
            old = Operation.model_validate(previous.payload)
            if old.status == "complete" or (
                old.request_hash,
                old.source_hash,
                old.session_id,
                old.operation_id,
                previous.owner_id,
                previous.data_source_set_id,
            ) != (
                value.request_hash,
                value.source_hash,
                value.session_id,
                value.operation_id,
                user,
                source,
            ):
                raise AnalysisOrchestrationError("operation_immutable")
        record = AgentRecord(
            id=record_id(user, value.session_id, value.operation_id),
            kind="operation",
            owner_id=user,
            data_source_set_id=source,
            revision=revision + 1,
            payload=value.model_dump(mode="json"),
        )
        if not await self.store.save(record, revision):
            raise AnalysisOrchestrationError("operation_revision_conflict")
        return record

    async def prepare(
        self, user: str, source: str, session: str, workspace: AgentWorkspace
    ) -> None:
        if not workspace.operation_id:
            return
        record = await self.get(user, session, workspace.operation_id)
        if record is None:
            return  # Legacy checkpoints are still readable, not invented approvals.
        value = Operation.model_validate(record.payload)
        if value.status == "running":
            await self.save(
                user,
                source,
                value.model_copy(
                    update={
                        "status": "prepared",
                        "workspace_json": workspace.model_dump_json(),
                    }
                ),
                record,
            )
