"""Compare-and-swap checkpoints; Firestore owns concurrency across workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Protocol

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from analytics_ai.agent.models import AgentRecord


def record_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _validate_firestore_values(value: Any) -> None:
    """Standard edition forbids an array directly containing another array."""
    if isinstance(value, list):
        if any(isinstance(item, list) for item in value):
            raise ValueError("agent_checkpoint_nested_array")
        for item in value:
            _validate_firestore_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_firestore_values(item)


def _encode_record(record: AgentRecord) -> dict[str, Any]:
    document = record.model_dump(mode="json")
    if record.kind == "workspace":
        payload = document["payload"]
        if "required_dimension_groups" in payload:
            # Keep the application/SSE contract unchanged. Only the persistence
            # boundary uses array -> map -> array instead of nested arrays.
            payload["required_dimension_groups"] = [
                {"fields": group} for group in payload["required_dimension_groups"]
            ]
    _validate_firestore_values(document)
    if len(json.dumps(document).encode()) > 500_000:
        raise ValueError("agent_checkpoint_too_large")
    document["due_at"] = record.due_at
    return document


def _decode_record(document: dict[str, Any]) -> AgentRecord:
    data = dict(document)
    if data.get("kind") == "workspace":
        payload = dict(data["payload"])
        if "required_dimension_groups" in payload:
            groups = []
            for group in payload["required_dimension_groups"]:
                if isinstance(group, dict):
                    if set(group) != {"fields"}:
                        raise ValueError("invalid_checkpoint_dimension_group")
                    group = group["fields"]
                # Legacy empty / list-shaped records remain readable, without
                # accepting a malformed group as a weaker completion contract.
                if not isinstance(group, list) or not all(
                    isinstance(field, str) for field in group
                ):
                    raise ValueError("invalid_checkpoint_dimension_group")
                groups.append(group)
            payload["required_dimension_groups"] = groups
        data["payload"] = payload
    return AgentRecord.model_validate(data)


class AgentStore(Protocol):
    async def get(self, kind: str, key: str) -> AgentRecord | None: ...
    async def save(self, record: AgentRecord, expected: int) -> bool: ...
    async def list_owner(self, kind: str, owner: str) -> list[AgentRecord]: ...
    async def due(self, now: datetime) -> list[AgentRecord]: ...
    async def list_scope(
        self, kind: str, owner: str, source: str, limit: int
    ) -> tuple[list[AgentRecord], bool]: ...


class MemoryAgentStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], AgentRecord] = {}
        self.lock = asyncio.Lock()

    async def get(self, kind: str, key: str) -> AgentRecord | None:
        return self.records.get((kind, key))

    async def save(self, record: AgentRecord, expected: int) -> bool:
        document = _encode_record(record)
        async with self.lock:
            current = self.records.get((record.kind, record.id))
            if (
                current.revision if current else 0
            ) != expected or record.revision != expected + 1:
                return False
            if current and (
                current.owner_id != record.owner_id
                or current.data_source_set_id != record.data_source_set_id
            ):
                raise ValueError("agent_record_scope_change")
            self.records[(record.kind, record.id)] = _decode_record(document)
            return True

    async def list_owner(self, kind: str, owner: str) -> list[AgentRecord]:
        return [
            r for r in self.records.values() if r.kind == kind and r.owner_id == owner
        ]

    async def due(self, now: datetime) -> list[AgentRecord]:
        return sorted(
            (
                r
                for r in self.records.values()
                if r.kind == "schedule" and r.due_at and r.due_at <= now
            ),
            key=lambda r: r.due_at or now,
        )[:20]

    async def list_scope(
        self, kind: str, owner: str, source: str, limit: int
    ) -> tuple[list[AgentRecord], bool]:
        records = [
            r
            for r in self.records.values()
            if r.kind == kind and r.owner_id == owner and r.data_source_set_id == source
        ]
        return records[:limit], len(records) > limit


class FirestoreAgentStore:
    def __init__(self, project_id: str) -> None:
        self.client = firestore.AsyncClient(project=project_id)

    def collection(self, kind: str):  # type: ignore[no-untyped-def]
        if kind not in {
            "site",
            "preferences",
            "workspace",
            "schedule",
            "agreements",
            "episode",
            "operation",
        }:
            raise ValueError("invalid_agent_record_kind")
        return self.client.collection(f"agent_{kind}")

    async def get(self, kind: str, key: str) -> AgentRecord | None:
        snapshot = await self.collection(kind).document(key).get()
        return _decode_record(snapshot.to_dict()) if snapshot.exists else None

    async def save(self, record: AgentRecord, expected: int) -> bool:
        payload = _encode_record(record)
        reference = self.collection(record.kind).document(record.id)

        @firestore.async_transactional
        async def commit(transaction: firestore.AsyncTransaction) -> bool:
            snapshot = await reference.get(transaction=transaction)
            current = _decode_record(snapshot.to_dict()) if snapshot.exists else None
            if (
                current.revision if current else 0
            ) != expected or record.revision != expected + 1:
                return False
            if current and (
                current.owner_id != record.owner_id
                or current.data_source_set_id != record.data_source_set_id
            ):
                raise ValueError("agent_record_scope_change")
            transaction.set(reference, payload)
            return True

        try:
            return bool(await commit(self.client.transaction()))
        except GoogleAPICallError as error:
            # Provider messages may contain document data; never log them.
            logging.getLogger(__name__).warning(
                "Agent checkpoint storage failed",
                extra={
                    "error_code": "agent_checkpoint_write_failed",
                    "error_type": type(error).__name__,
                },
            )
            raise

    async def list_owner(self, kind: str, owner: str) -> list[AgentRecord]:
        query = (
            self.collection(kind)
            .where(filter=FieldFilter("owner_id", "==", owner))
            .limit(500)
        )
        return [_decode_record(s.to_dict()) async for s in query.stream()]

    async def due(self, now: datetime) -> list[AgentRecord]:
        query = (
            self.collection("schedule")
            .where(filter=FieldFilter("due_at", "<=", now))
            .order_by("due_at")
            .limit(20)
        )
        return [_decode_record(s.to_dict()) async for s in query.stream()]

    async def list_scope(
        self, kind: str, owner: str, source: str, limit: int
    ) -> tuple[list[AgentRecord], bool]:
        query = (
            self.collection(kind)
            .where(filter=FieldFilter("owner_id", "==", owner))
            .where(filter=FieldFilter("data_source_set_id", "==", source))
            .limit(limit + 1)
        )
        records = [_decode_record(s.to_dict()) async for s in query.stream()]
        return records[:limit], len(records) > limit
