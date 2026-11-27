"""Read-only operator check, not a substitute for two real Firebase logins."""

import asyncio
import json

from analytics_ai.api.app import build_services
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.config import Settings

USER = "example-user-uid"
OTHER = "example-other-uid"
SETS = ("example_active_source_a", "example_active_source_b")
APPROVED_PROPERTY_ID = "100000001"
APPROVED_STREAM_ID = "200000001"


async def verify() -> None:
    settings = Settings()
    if settings.environment != "local" or not settings.firebase_project_id:
        raise RuntimeError("acceptance_environment_mismatch")
    services = build_services(settings)
    rows = []
    for source_id in SETS:
        scope = await services.authorization.authorize_source_set(USER, source_id)
        assert (
            scope.source_set.ga4
            and scope.source_set.ga4.property_id == APPROVED_PROPERTY_ID
        )
        assert [
            (s.stream_id, s.platform.value) for s in scope.source_set.ga4.stream_scopes
        ] == [(APPROVED_STREAM_ID, "web")]
        grants = await services.repository.list_source_grants(source_id)
        assert len([g for g in grants if g.enabled]) == 1
        assert all(g.id.startswith("managed_") for g in grants if g.enabled)
        registration = await services.repository.get_registration(source_id)
        assert registration and registration.status == "active" and registration.proof
        try:
            await services.authorization.authorize_source_set(OTHER, source_id)
        except AccessDenied:
            other_denied = True
        else:
            raise RuntimeError("other_user_unexpectedly_authorized")
        rows.append(
            {
                "set": source_id,
                "status": registration.status,
                "other_user_denied": other_denied,
                "explicit_grants": 1,
                "stream_metadata_parent": registration.proof.stream_metadata_parent,
            }
        )
    for source_id in ("example_legacy_source", "example_disabled_source"):
        try:
            await services.authorization.authorize_source_set(USER, source_id)
        except AccessDenied:
            rows.append({"set": source_id, "disabled_access_denied": True})
        else:
            raise RuntimeError("disabled_set_unexpectedly_authorized")
    sessions = await services.repository.list_sessions(USER)
    legacy_denied = 0
    for legacy in sessions:
        if (
            legacy.data_source_set_id != SETS[0]
            or legacy.source_scope_fingerprint
            or legacy.status.value != "active"
        ):
            continue
        try:
            await services.agent.session(USER, legacy.id)
        except AccessDenied as denied:
            assert denied.internal_reason == "legacy_session_source_scope_unverified"
            legacy_denied += 1
        else:
            raise RuntimeError("broader_legacy_history_unexpectedly_authorized")
    q2b = next(s for s in sessions if s.title == "Q2b Web限定検証 2026-09-24")
    await services.agent.session(USER, q2b.id)
    messages = await services.repository.list_messages(q2b.id)
    assert len([m for m in messages if m.role.value == "assistant"]) >= 5
    output = {
        "checks": rows,
        "q2b_session": q2b.id,
        "saved_messages": len(messages),
        "broader_legacy_sessions_retained_but_denied": legacy_denied,
        "firebase_login_e2e": "not_performed",
        "additional_ga4_queries": 0,
        "mutations": 0,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(verify())
