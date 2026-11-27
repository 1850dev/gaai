"""Legacy approval retains identity/history and never carries implicit grants."""

import pytest

from analytics_ai.admin.models import (
    GrantRequest,
    LegacyRegistrationRequest,
    RegistrationError,
    RegistrationInput,
)
from analytics_ai.admin.policy import fingerprint, grants_fingerprint
from analytics_ai.admin.service import RegistrationService
from analytics_ai.domain.models import DataSourceSet, Ga4Source, UserAccessGrant
from tests.test_admin_registration import BASE, DEFINITION
from tests.test_admin_registration import setup as registration_setup
from tests.test_api_authorization import auth


@pytest.fixture
def setup(repository):
    return registration_setup.__wrapped__(repository)


def legacy(repo):
    source = DataSourceSet(
        id="legacy_web",
        company_id="company_a",
        name="Old web",
        ga4=Ga4Source(
            property_id="222222222", display_name="Web", auth_mode="USER_OAUTH"
        ),
        semantic_version="preserve-version",
        enabled=True,
    )
    repo.source_sets[source.id] = source
    grant = UserAccessGrant(
        id="old_auto_grant", user_id="user-a", data_source_set_id=source.id
    )
    repo.grants[(grant.user_id, source.id)] = grant
    return source, grant


def body(repo, source, definition=DEFINITION):
    return {
        "definition": definition,
        "source_hash": fingerprint(source),
        "grants_hash": grants_fingerprint(
            [g for g in repo.grants.values() if g.data_source_set_id == source.id]
        ),
    }


def test_inventory_requires_exact_company_admin(setup):
    repo, _, api = setup
    source, _ = legacy(repo)
    path = "/api/admin/companies/company_a/data-source-inventory"
    assert api.get(path, headers=auth("user-a")).status_code == 403
    response = api.get(path, headers=auth("admin-a"))
    assert response.status_code == 200
    item = next(
        x for x in response.json()["items"] if x["source_set"]["id"] == source.id
    )
    assert item["registration"] is None
    assert item["grants"][0]["id"] == "old_auto_grant"
    assert (
        api.get(path + "?after=../escape", headers=auth("admin-a")).status_code == 422
    )


@pytest.mark.asyncio
async def test_migration_disables_old_grant_preserves_semantics_and_history(setup):
    repo, verifier, _ = setup
    source, grant = legacy(repo)
    semantics = dict(repo.semantic_definitions)
    sessions = dict(repo.sessions)
    service = RegistrationService(repo, verifier)
    staged = await service.stage_legacy(
        "admin-a",
        "company_a",
        source.id,
        LegacyRegistrationRequest.model_validate(body(repo, source)),
        "stage",
    )
    assert repo.source_sets[source.id] == source  # draft does not publish
    verified = await service.verify(
        "admin-a", "company_a", source.id, staged.revision, "verify"
    )
    active = await service.activate(
        "admin-a", "company_a", source.id, verified.revision, "activate"
    )
    assert active.source_set.id == source.id
    assert active.source_set.semantic_version == source.semantic_version
    assert repo.semantic_definitions == semantics and repo.sessions == sessions
    assert not repo.grants[(grant.user_id, source.id)].enabled
    assert ("admin-a", source.id) not in repo.grants
    await service.grant(
        "admin-a",
        "company_a",
        source.id,
        GrantRequest(expected_revision=active.revision, user_id="user-a", enabled=True),
        "grant",
    )
    assert repo.grants[("user-a", source.id)].enabled
    assert repo.grants[("user-a", source.id)].id.startswith("managed_")
    assert repo.audit_events[-2].details["legacy_grants_disabled"] is True


@pytest.mark.parametrize("change", ["source", "grants"])
@pytest.mark.asyncio
async def test_review_stale_during_verification_does_not_activate(setup, change):
    repo, verifier, _ = setup
    source, grant = legacy(repo)
    service = RegistrationService(repo, verifier)
    staged = await service.stage_legacy(
        "admin-a",
        "company_a",
        source.id,
        LegacyRegistrationRequest.model_validate(body(repo, source)),
        "stage",
    )
    if change == "source":
        verifier.mutate = lambda: repo.source_sets.update(
            {source.id: source.model_copy(update={"name": "changed"})}
        )
    else:
        verifier.mutate = lambda: repo.grants.update(
            {("user-a", source.id): grant.model_copy(update={"enabled": False})}
        )
    with pytest.raises(RegistrationError, match="registration_legacy_review_stale"):
        await service.verify(
            "admin-a", "company_a", source.id, staged.revision, "verify"
        )
    assert repo.registrations[source.id].status == "draft"


@pytest.mark.parametrize("change", ["property", "auth", "stream"])
def test_no_scope_escalation(setup, change):
    repo, _, api = setup
    source, _ = legacy(repo)
    source = source.model_copy(
        update={"ga4": RegistrationInput.model_validate(DEFINITION).ga4}
    )
    repo.source_sets[source.id] = source
    definition = RegistrationInput.model_validate(DEFINITION).model_dump(mode="json")
    if change == "property":
        definition["ga4"]["property_id"] = "999999999"
    if change == "auth":
        definition["ga4"]["auth_mode"] = "SERVICE_ACCOUNT"
        definition["data_access_profile_id"] = "profile_a"
    if change == "stream":
        definition["ga4"]["stream_scopes"][0]["stream_id"] = "9999999999"
    response = api.post(
        f"{BASE}/{source.id}/legacy",
        json=body(repo, source, definition),
        headers=auth("admin-a"),
    )
    assert response.status_code in {403, 409}
    assert source.id not in repo.registrations


def test_disable_preserves_source_and_rejects_reenable(setup):
    repo, _, api = setup
    source, _ = legacy(repo)
    payload = body(repo, source)
    payload.pop("definition")
    response = api.post(
        f"{BASE}/{source.id}/disable", json=payload, headers=auth("admin-a")
    )
    assert response.status_code == 200, response.text
    assert not repo.source_sets[source.id].enabled
    assert not repo.grants[("user-a", source.id)].enabled
    assert (
        api.post(
            f"{BASE}/{source.id}/verify",
            json={"expected_revision": 1},
            headers=auth("admin-a"),
        ).status_code
        == 409
    )
