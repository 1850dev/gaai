"""The four exact targets approved by the user on 2026-09-30, not a bulk migration."""

import argparse
import asyncio
import json

from analytics_ai.admin.models import (
    DisableRegistrationRequest,
    GrantRequest,
    LegacyRegistrationRequest,
    RegistrationInput,
)
from analytics_ai.admin.policy import fingerprint, grants_fingerprint
from analytics_ai.api.app import build_services
from analytics_ai.config import Settings

ACTOR = "example-admin-uid"
COMPANY = "example-company"
APPROVE = ("example_active_source_a", "example_active_source_b")
DISABLE = ("example_legacy_source", "example_disabled_source")
APPROVED_PROPERTY_ID = "100000001"
APPROVED_STREAM_ID = "200000001"


async def migrate() -> None:
    settings = Settings(enable_admin_connection_checks=True)
    if settings.environment != "local" or not settings.firebase_project_id:
        raise RuntimeError("approved_environment_mismatch")
    services = build_services(settings)
    service = services.registrations
    assert service is not None
    await service.authorize(ACTOR, COMPANY)
    output = []
    for source_id in APPROVE:
        old = await service.legacy_source(ACTOR, COMPANY, source_id)
        assert (
            old.ga4
            and old.ga4.property_id == APPROVED_PROPERTY_ID
            and old.ga4.auth_mode.value == "USER_OAUTH"
        )
        grants = await services.repository.list_source_grants(source_id)
        if len(grants) > 100 or any(g.user_id != ACTOR for g in grants):
            raise RuntimeError("approved_grant_scope_changed")
        registration = await services.repository.get_registration(source_id)
        if registration is None:
            definition = RegistrationInput.model_validate(
                {
                    "name": old.name,
                    "ga4": {
                        **old.ga4.model_dump(mode="json"),
                        "stream_scopes": [
                            {"stream_id": APPROVED_STREAM_ID, "platform": "web"}
                        ],
                    },
                }
            )
            registration = await service.stage_legacy(
                ACTOR,
                COMPANY,
                source_id,
                LegacyRegistrationRequest(
                    definition=definition,
                    source_hash=fingerprint(old),
                    grants_hash=grants_fingerprint(grants),
                ),
                "s1c-approved-20260930-stage",
            )
        if registration.status in {"draft", "verified"}:
            desired = registration.source_set
            if (
                desired.id != source_id
                or desired.company_id != COMPANY
                or not desired.ga4
                or desired.ga4.property_id != APPROVED_PROPERTY_ID
                or desired.ga4.auth_mode.value != "USER_OAUTH"
                or [(s.stream_id, s.platform.value) for s in desired.ga4.stream_scopes]
                != [(APPROVED_STREAM_ID, "web")]
            ):
                raise RuntimeError("approved_registration_scope_changed")
            registration = await service.verify(
                ACTOR,
                COMPANY,
                source_id,
                registration.revision,
                "s1c-approved-20260930-verify",
            )
            registration = await service.activate(
                ACTOR,
                COMPANY,
                source_id,
                registration.revision,
                "s1c-approved-20260930-activate",
            )
        if registration.status != "active":
            raise RuntimeError("approved_registration_not_active")
        if (
            not registration.source_set.ga4
            or registration.source_set.ga4.property_id != APPROVED_PROPERTY_ID
            or registration.source_set.ga4.auth_mode.value != "USER_OAUTH"
            or [
                (s.stream_id, s.platform.value)
                for s in registration.source_set.ga4.stream_scopes
            ]
            != [(APPROVED_STREAM_ID, "web")]
        ):
            raise RuntimeError("approved_active_scope_changed")
        await service.grant(
            ACTOR,
            COMPANY,
            source_id,
            GrantRequest(
                expected_revision=registration.revision, user_id=ACTOR, enabled=True
            ),
            "s1c-approved-20260930-grant",
        )
        output.append(
            {
                "id": source_id,
                "status": "active",
                "old_grants_disabled": True,
                "property": APPROVED_PROPERTY_ID,
                "web_stream": APPROVED_STREAM_ID,
                "explicit_uid": ACTOR,
            }
        )
    for source_id in DISABLE:
        old = await service.legacy_source(ACTOR, COMPANY, source_id)
        expected_property = (
            "100000002" if source_id == "example_legacy_source" else "100000003"
        )
        if not old.ga4 or old.ga4.property_id != expected_property:
            raise RuntimeError("approved_disable_scope_changed")
        grants = await services.repository.list_source_grants(source_id)
        if len(grants) > 100 or any(g.user_id != ACTOR for g in grants):
            raise RuntimeError("approved_grant_scope_changed")
        registration = await services.repository.get_registration(source_id)
        if registration is None or registration.status != "disabled":
            await service.disable(
                ACTOR,
                COMPANY,
                source_id,
                DisableRegistrationRequest(
                    source_hash=fingerprint(old),
                    grants_hash=grants_fingerprint(grants),
                    expected_revision=registration.revision if registration else None,
                ),
                "s1c-approved-20260930-disable",
            )
        output.append(
            {"id": source_id, "status": "disabled", "old_grants_disabled": True}
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute-approved-20260930", required=True, action="store_true"
    )
    parser.parse_args()
    asyncio.run(migrate())
