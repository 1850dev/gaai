"""Checkpoint contract tests against Firestore Standard's value restrictions."""

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from google.api_core.exceptions import InvalidArgument

from analytics_ai.agent.models import AgentRecord, AgentWorkspace
from analytics_ai.agent.store import (
    FirestoreAgentStore,
    MemoryAgentStore,
    _decode_record,
    _encode_record,
)


def assert_firestore_values(value):
    if isinstance(value, list):
        if any(isinstance(item, list) for item in value):
            raise InvalidArgument("Cannot convert an array value in an array value")
        for item in value:
            assert_firestore_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            assert_firestore_values(item)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "groups",
    [
        (),
        (("pagePathPlusQueryString", "sessionSource"),),
        (("country", "deviceCategory"), ("pagePath", "sessionSource", "browser")),
    ],
)
async def test_firestore_checkpoint_groups_roundtrip(monkeypatch, groups) -> None:
    document = Mock()
    snapshot = Mock(exists=False)
    document.get = AsyncMock(return_value=snapshot)
    transaction = Mock()
    captured = {}

    def save(reference, payload):
        assert reference is document
        assert_firestore_values(payload)
        captured.update(deepcopy(payload))

    transaction.set.side_effect = save
    firestore_client = Mock()
    firestore_client.collection.return_value.document.return_value = document
    firestore_client.transaction.return_value = transaction
    monkeypatch.setattr(
        "analytics_ai.agent.store.firestore.AsyncClient",
        Mock(return_value=firestore_client),
    )
    monkeypatch.setattr(
        "analytics_ai.agent.store.firestore.async_transactional", lambda fn: fn
    )
    store = FirestoreAgentStore("test-project")
    workspace = AgentWorkspace(required_dimension_groups=groups)
    record = AgentRecord(
        id="checkpoint",
        kind="workspace",
        owner_id="user-a",
        data_source_set_id="source-a",
        revision=1,
        payload=workspace.model_dump(mode="json"),
    )
    assert await store.save(record, 0)
    assert captured["payload"]["required_dimension_groups"] == [
        {"fields": list(group)} for group in groups
    ]
    snapshot.exists = True
    snapshot.to_dict.return_value = captured
    restored = await store.get("workspace", record.id)
    assert restored == record
    assert AgentWorkspace.model_validate(restored.payload) == workspace
    # CAS must read the new storage shape without weakening revision checks.
    transaction.set.reset_mock()
    assert not await store.save(record, 0)
    transaction.set.assert_not_called()
    updated = record.model_copy(update={"revision": 2})
    assert await store.save(updated, 1)
    assert captured["revision"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"required_dimension_groups": []},
        {"required_dimension_groups": [["pagePath", "sessionSource"]]},
    ],
)
def test_legacy_checkpoint_is_readable_without_mutation(payload) -> None:
    record = AgentRecord(
        id="legacy",
        kind="workspace",
        owner_id="user-a",
        revision=1,
        data_source_set_id="source-a",
        payload=payload,
    )
    document = record.model_dump(mode="json")
    original = deepcopy(document)
    assert _decode_record(document) == record
    assert document == original
    assert _decode_record(_encode_record(record)) == record


@pytest.mark.asyncio
async def test_memory_store_checks_firestore_contract_and_preserves_scope() -> None:
    store = MemoryAgentStore()
    record = AgentRecord(
        id="checkpoint",
        kind="workspace",
        owner_id="user-a",
        revision=1,
        data_source_set_id="source-a",
        payload={"required_dimension_groups": [["pagePath", "sessionSource"]]},
    )
    assert await store.save(record, 0)
    assert await store.get("workspace", record.id) == record
    for changes in [{"owner_id": "user-b"}, {"data_source_set_id": "source-b"}]:
        with pytest.raises(ValueError, match="agent_record_scope_change"):
            await store.save(record.model_copy(update={**changes, "revision": 2}), 1)
    invalid = record.model_copy(
        update={"revision": 2, "payload": {"unexpected": [["private value"]]}}
    )
    with pytest.raises(ValueError, match=r"^agent_checkpoint_nested_array$"):
        await store.save(invalid, 1)
    assert await store.get("workspace", record.id) == record


@pytest.mark.parametrize("group", [{"unexpected": []}, {"fields": "pagePath"}])
def test_malformed_storage_group_is_not_silently_discarded(group) -> None:
    record = AgentRecord(
        id="checkpoint",
        kind="workspace",
        owner_id="user-a",
        revision=1,
        payload={"required_dimension_groups": [group]},
    )
    with pytest.raises(ValueError, match="invalid_checkpoint_dimension_group"):
        _decode_record(record.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_storage_failure_logs_stage_but_not_provider_message(
    monkeypatch, caplog
) -> None:
    firestore_client = Mock()
    firestore_client.collection.return_value.document.return_value.get = AsyncMock(
        side_effect=InvalidArgument("private prompt and document contents")
    )
    monkeypatch.setattr(
        "analytics_ai.agent.store.firestore.AsyncClient",
        Mock(return_value=firestore_client),
    )
    monkeypatch.setattr(
        "analytics_ai.agent.store.firestore.async_transactional", lambda fn: fn
    )
    store = FirestoreAgentStore("test-project")
    record = AgentRecord(
        id="checkpoint", kind="workspace", owner_id="user-a", revision=1, payload={}
    )
    with pytest.raises(InvalidArgument):
        await store.save(record, 0)
    assert "private prompt" not in caplog.text
    assert caplog.records[-1].error_code == "agent_checkpoint_write_failed"
    assert caplog.records[-1].error_type == "InvalidArgument"
