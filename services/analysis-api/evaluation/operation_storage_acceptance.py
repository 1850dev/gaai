"""One isolated synthetic operation: actual Firestore CAS and process restoration."""

import argparse
import asyncio
import json
import uuid
from datetime import timedelta

from analytics_ai.agent.models import AgentWorkspace
from analytics_ai.agent.operations import Operation, OperationLedger
from analytics_ai.agent.store import FirestoreAgentStore
from analytics_ai.config import Settings
from analytics_ai.domain.models import utc_now


async def verify() -> None:
    settings = Settings()
    if settings.environment != "local" or not settings.firebase_project_id:
        raise RuntimeError("acceptance_environment_mismatch")
    nonce = uuid.uuid4().hex
    user, source, session = (
        f"acceptance_{nonce}",
        f"acceptance_source_{nonce}",
        f"acceptance_session_{nonce}",
    )
    store = FirestoreAgentStore(settings.firebase_project_id)
    ledger = OperationLedger(store)
    value = Operation(
        session_id=session,
        operation_id=f"storage-{nonce}",
        request_hash="synthetic",
        source_hash="synthetic",
        semantic_version="synthetic",
        status="prepared",
        lease_until=utc_now() + timedelta(minutes=1),
        workspace_json=AgentWorkspace(
            required_dimension_groups=(("pagePath", "sessionSource"),)
        ).model_dump_json(),
    )
    record = await ledger.save(user, source, value, None)
    try:
        restored = await OperationLedger(
            FirestoreAgentStore(settings.firebase_project_id)
        ).get(user, session, value.operation_id)
        assert restored == record
        assert not await store.save(record, 0)
        assert AgentWorkspace.model_validate_json(
            Operation.model_validate(restored.payload).workspace_json
        ).required_dimension_groups == (("pagePath", "sessionSource"),)
        print(
            json.dumps(
                {
                    "actual_firestore_roundtrip": True,
                    "stale_cas_rejected": True,
                    "separate_client_restore": True,
                    "real_session_mutations": 0,
                    "synthetic_records_created": 1,
                }
            )
        )
    finally:
        reference = store.collection("operation").document(record.id)
        current = await reference.get()
        if current.exists:
            payload = current.to_dict()
            if (
                payload.get("owner_id") != user
                or payload.get("data_source_set_id") != source
            ):
                raise RuntimeError("acceptance_cleanup_scope_changed")
            await reference.delete()
        assert not (await reference.get()).exists
        print(json.dumps({"synthetic_records_removed": 1, "real_session_mutations": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-one-synthetic-record", required=True, action="store_true"
    )
    parser.parse_args()
    asyncio.run(verify())
