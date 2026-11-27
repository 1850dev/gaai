from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from analytics_ai.admin.models import RegistrationError, RegistrationInput
from analytics_ai.admin.service import RegistrationService
from analytics_ai.api.app import create_app
from analytics_ai.config import Settings
from analytics_ai.domain.models import (
    Company,
    CompanyMember,
    MemberRole,
    Status,
    User,
    utc_now,
)
from tests.test_api_authorization import StaticVerifier, auth

BASE = "/api/admin/companies/company_a/data-source-registrations"
DEFINITION = {
    "name": "Web registered",
    "ga4": {
        "property_id": "222222222",
        "display_name": "Web",
        "auth_mode": "USER_OAUTH",
        "timezone": "Asia/Tokyo",
        "currency_code": "JPY",
        "stream_scopes": [{"stream_id": "3333333333", "platform": "web"}],
    },
}


class Verifier:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.mutate = None

    async def verify(self, **kwargs):
        self.calls.append(kwargs)
        if self.mutate:
            self.mutate()
        if self.fail:
            raise RuntimeError("secret upstream message must not escape")
        return "synthetic-google-subject"


@pytest.fixture
def setup(repository):
    repository.companies["company_a"] = Company(id="company_a", name="A")
    repository.companies["company_b"] = Company(id="company_b", name="B")
    repository.users["admin-a"] = User(uid="admin-a", email="admin@example.com")
    repository.members[("company_a", "admin-a")] = CompanyMember(
        user_id="admin-a", company_id="company_a", role=MemberRole.COMPANY_ADMIN
    )
    verifier = Verifier()
    api = TestClient(
        create_app(
            Settings(environment="test", enable_admin_connection_checks=True),
            repository=repository,
            token_verifier=StaticVerifier(),
            registration_verifier=verifier,
        )
    )
    return repository, verifier, api


def create(api):
    response = api.post(BASE, json=DEFINITION, headers=auth("admin-a"))
    assert response.status_code == 201, response.text
    return response.json()


def path(record):
    return f"{BASE}/{record['source_set']['id']}"


def transition(api, record, action):
    return api.post(
        f"{path(record)}/{action}",
        json={"expected_revision": record["revision"]},
        headers=auth("admin-a"),
    )


def activate(api):
    draft = create(api)
    verified = transition(api, draft, "verify")
    assert verified.status_code == 200, verified.text
    active = transition(api, verified.json(), "activate")
    assert active.status_code == 200, active.text
    return active.json()


def test_draft_verify_activate_never_grants_analysis(setup):
    repo, verifier, api = setup
    grants, sources = dict(repo.grants), dict(repo.source_sets)
    draft = create(api)
    source_id = draft["source_set"]["id"]
    assert draft["status"] == "draft"
    assert source_id not in repo.source_sets
    assert repo.grants == grants
    assert transition(api, draft, "activate").status_code == 409
    result = transition(api, draft, "verify")
    assert result.status_code == 200, result.text
    verified = result.json()
    assert verified["status"] == "verified"
    assert not verified["source_set"]["enabled"]
    assert verified["proof"]["credential_identity"] == "synthetic-google-subject"
    assert repo.source_sets == sources
    assert len(verifier.calls) == 1
    assert verifier.calls[0]["source_set"].ga4.property_id == "222222222"
    active = transition(api, verified, "activate").json()
    assert repo.source_sets[source_id].enabled
    assert repo.grants == grants
    assert all(g.data_source_set_id != source_id for g in repo.grants.values())
    assert any(
        s.data_source_set_id == source_id for s in repo.semantic_definitions.values()
    )
    for user in ("admin-a", "user-a", "user-b"):
        assert (
            api.post(
                "/api/sessions",
                json={"data_source_set_id": source_id},
                headers=auth(user),
            ).status_code
            == 403
        )
    granted = api.put(
        f"{path(active)}/grant",
        json={
            "expected_revision": active["revision"],
            "user_id": "user-a",
            "enabled": True,
        },
        headers=auth("admin-a"),
    )
    assert granted.status_code == 200, granted.text
    assert (
        api.post(
            "/api/sessions",
            json={"data_source_set_id": source_id},
            headers=auth("user-a"),
        ).status_code
        == 201
    )
    assert (
        api.post(
            "/api/sessions",
            json={"data_source_set_id": source_id},
            headers=auth("user-b"),
        ).status_code
        == 403
    )
    revoked = api.put(
        f"{path(active)}/grant",
        json={
            "expected_revision": granted.json()["revision"],
            "user_id": "user-a",
            "enabled": False,
        },
        headers=auth("admin-a"),
    )
    assert revoked.status_code == 200
    assert (
        api.post(
            "/api/sessions",
            json={"data_source_set_id": source_id},
            headers=auth("user-a"),
        ).status_code
        == 403
    )
    actions = [a.action for a in repo.audit_events]
    assert "admin.registration.activated" in actions
    assert actions.count("admin.registration.grant_changed") == 2
    assert api.get(path(active), headers=auth("user-a")).status_code == 403


@pytest.mark.parametrize(
    "field", ["timezone", "currency_code", "stream_scopes", "auth_mode"]
)
def test_registration_requires_explicit_scope(setup, field):
    repo, _, api = setup
    payload = {
        **DEFINITION,
        "ga4": {k: v for k, v in DEFINITION["ga4"].items() if k != field},
    }
    assert api.post(BASE, json=payload, headers=auth("admin-a")).status_code == 422
    assert not repo.registrations


def test_second_company_admin_can_onboard_own_users_but_not_access_first(setup):
    repo, _, api = setup
    for uid, role in (
        ("admin-b", MemberRole.COMPANY_ADMIN),
        ("analyst-b", MemberRole.ANALYST),
    ):
        repo.users[uid] = User(uid=uid, email=f"{uid}@second-tenant.example")
        repo.members[("company_b", uid)] = CompanyMember(
            user_id=uid, company_id="company_b", role=role
        )

    admin = auth("admin-b")
    assert api.get("/api/me", headers=admin).json()["can_manage_data_sources"] is True
    assert (
        not api.get("/api/me", headers=auth("analyst-b"))
        .json()
        .get("can_manage_data_sources", False)
    )
    disabled = TestClient(
        create_app(
            Settings(environment="test"),
            repository=repo,
            token_verifier=StaticVerifier(),
        )
    )
    assert disabled.get("/api/me", headers=admin).status_code == 200
    assert (
        not disabled.get("/api/me", headers=admin)
        .json()
        .get("can_manage_data_sources", False)
    )
    assert disabled.get("/api/admin/companies", headers=admin).status_code == 503
    assert [c["id"] for c in api.get("/api/admin/companies", headers=admin).json()] == [
        "company_b"
    ]
    assert (
        api.get(
            "/api/admin/companies/company_a/data-source-inventory", headers=admin
        ).status_code
        == 403
    )
    base = BASE.replace("company_a", "company_b")
    definition = {
        **DEFINITION,
        "ga4": {
            **DEFINITION["ga4"],
            "property_id": "777777777",
            "stream_scopes": [{"stream_id": "8888888888", "platform": "web"}],
        },
    }
    created = api.post(base, json=definition, headers=admin)
    assert created.status_code == 201, created.text
    record = created.json()
    source_id = record["source_set"]["id"]
    source_path = f"{base}/{source_id}"
    for action in ("verify", "activate"):
        response = api.post(
            f"{source_path}/{action}",
            json={"expected_revision": record["revision"]},
            headers=admin,
        )
        assert response.status_code == 200, response.text
        record = response.json()
    assert api.get("/api/data-source-sets", headers=auth("analyst-b")).json() == []
    granted = api.put(
        f"{source_path}/grant",
        json={
            "expected_revision": record["revision"],
            "user_id": "analyst-b",
            "enabled": True,
        },
        headers=admin,
    )
    assert granted.status_code == 200, granted.text
    visible = api.get("/api/data-source-sets", headers=auth("analyst-b")).json()
    assert [item["id"] for item in visible] == [source_id]
    assert visible[0]["ga4_property_id"] == "777777777"
    for uid, source, code in (
        ("analyst-b", source_id, 201),
        ("analyst-b", "company_a_web", 403),
        ("user-a", source_id, 403),
    ):
        response = api.post(
            "/api/sessions", json={"data_source_set_id": source}, headers=auth(uid)
        )
        assert response.status_code == code


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream_scopes", []),
        ("stream_scopes", [{"stream_id": "G-tag", "platform": "web"}]),
        ("stream_scopes", [{"stream_id": "12", "platform": "web"}] * 2),
        ("timezone", "not/a-timezone"),
        ("currency_code", "yen"),
        ("property_id", "properties/222222222"),
        ("auth_mode", "AUTO"),
    ],
)
def test_registration_rejects_invalid_scope(setup, field, value):
    repo, _, api = setup
    payload = {**DEFINITION, "ga4": {**DEFINITION["ga4"], field: value}}
    assert api.post(BASE, json=payload, headers=auth("admin-a")).status_code == 422
    assert not repo.registrations


@pytest.mark.parametrize(
    "extra",
    [
        {"enabled": True},
        {"company_id": "company_b"},
        {"semantic_version": "arbitrary"},
        {"user_id": "user-a"},
        {"proof": {}},
        {"bigquery": {}},
    ],
)
def test_create_cannot_smuggle_authorization_fields(setup, extra):
    repo, _, api = setup
    assert (
        api.post(
            BASE, json={**DEFINITION, **extra}, headers=auth("admin-a")
        ).status_code
        == 422
    )
    assert not repo.registrations


@pytest.mark.parametrize("role", [MemberRole.COMPANY_ADMIN, MemberRole.PLATFORM_ADMIN])
def test_platform_and_company_admin_are_scoped_to_membership(setup, role):
    repo, _, api = setup
    repo.members[("company_a", "admin-a")] = repo.members[
        ("company_a", "admin-a")
    ].model_copy(update={"role": role})
    denied = api.post(
        BASE.replace("company_a", "company_b"), json=DEFINITION, headers=auth("admin-a")
    )
    assert denied.status_code == 403
    assert not repo.registrations
    assert repo.audit_events[-1].status == "denied"
    create(api)


@pytest.mark.parametrize("disabled", ["user", "member", "company", "analyst"])
def test_inactive_or_nonadmin_cannot_register(setup, disabled):
    repo, _, api = setup
    if disabled == "user":
        repo.users["admin-a"] = repo.users["admin-a"].model_copy(
            update={"status": Status.DISABLED}
        )
    elif disabled == "company":
        repo.companies["company_a"] = repo.companies["company_a"].model_copy(
            update={"status": Status.DISABLED}
        )
    else:
        update = (
            {"role": MemberRole.ANALYST}
            if disabled == "analyst"
            else {"status": Status.DISABLED}
        )
        repo.members[("company_a", "admin-a")] = repo.members[
            ("company_a", "admin-a")
        ].model_copy(update=update)
    assert api.post(BASE, json=DEFINITION, headers=auth("admin-a")).status_code == 403
    assert not repo.registrations


def test_admin_gate_and_firebase_auth_fail_closed(setup):
    repo, verifier, api = setup
    assert api.post(BASE, json=DEFINITION).status_code == 401
    gated = TestClient(
        create_app(
            Settings(environment="test"),
            repository=repo,
            token_verifier=StaticVerifier(),
            registration_verifier=verifier,
        )
    )
    assert gated.post(BASE, json=DEFINITION, headers=auth("admin-a")).status_code == 503
    assert not repo.registrations and not verifier.calls


def test_update_clears_verification_and_stale_revision_cannot_activate(setup):
    _, _, api = setup
    verified = transition(api, create(api), "verify").json()
    changed = api.put(
        path(verified),
        headers=auth("admin-a"),
        json={
            "expected_revision": verified["revision"],
            "definition": {**DEFINITION, "name": "changed"},
        },
    )
    assert changed.status_code == 200
    assert changed.json()["proof"] is None
    assert changed.json()["status"] == "draft"
    assert transition(api, verified, "activate").status_code == 409
    assert transition(api, changed.json(), "activate").status_code == 409


def test_failed_reverification_cannot_reuse_old_success_or_leak_error(setup):
    repo, verifier, api = setup
    verified = transition(api, create(api), "verify").json()
    verifier.fail = True
    failed = transition(api, verified, "verify")
    assert failed.status_code == 503
    assert "secret upstream" not in failed.text
    latest = api.get(path(verified), headers=auth("admin-a")).json()
    assert latest["status"] == "draft" and latest["proof"] is None
    assert transition(api, latest, "activate").status_code == 409
    assert "secret upstream" not in str(repo.audit_events)


@pytest.mark.parametrize("mutation", ["revision", "role"])
def test_verification_does_not_publish_after_concurrent_change(setup, mutation):
    repo, verifier, api = setup
    draft = create(api)
    source_id = draft["source_set"]["id"]

    def change():
        if mutation == "revision":
            record = repo.registrations[source_id]
            repo.registrations[source_id] = record.model_copy(
                update={"revision": record.revision + 1}
            )
        else:
            repo.members[("company_a", "admin-a")] = repo.members[
                ("company_a", "admin-a")
            ].model_copy(update={"role": MemberRole.ANALYST})

    verifier.mutate = change
    response = transition(api, draft, "verify")
    assert response.status_code == (409 if mutation == "revision" else 403)
    assert source_id not in repo.source_sets
    assert repo.registrations[source_id].proof is None


def test_expired_proof_cannot_activate(setup):
    repo, _, api = setup
    verified = transition(api, create(api), "verify").json()
    source_id = verified["source_set"]["id"]
    record = repo.registrations[source_id]
    repo.registrations[source_id] = record.model_copy(
        update={
            "proof": record.proof.model_copy(
                update={"verified_at": utc_now() - timedelta(minutes=16)}
            )
        }
    )
    assert transition(api, verified, "activate").status_code == 409
    assert source_id not in repo.source_sets


def test_active_registration_is_immutable_and_grant_requires_active(setup):
    _, _, api = setup
    draft = create(api)
    assert (
        api.put(
            f"{path(draft)}/grant",
            headers=auth("admin-a"),
            json={
                "expected_revision": draft["revision"],
                "user_id": "user-a",
                "enabled": True,
            },
        ).status_code
        == 409
    )
    active = activate(api)
    for action in ("verify", "activate"):
        assert transition(api, active, action).status_code == 409
    assert (
        api.put(
            path(active),
            headers=auth("admin-a"),
            json={"expected_revision": active["revision"], "definition": DEFINITION},
        ).status_code
        == 409
    )


@pytest.mark.parametrize("recipient", ["missing", "disabled", "other_company"])
def test_grant_target_must_be_active_in_same_company(setup, recipient):
    repo, _, api = setup
    active = activate(api)
    user_id = "missing" if recipient == "missing" else "user-a"
    if recipient == "disabled":
        repo.users["user-a"] = repo.users["user-a"].model_copy(
            update={"status": Status.DISABLED}
        )
    elif recipient == "other_company":
        del repo.members[("company_a", "user-a")]
    old = dict(repo.grants)
    response = api.put(
        f"{path(active)}/grant",
        headers=auth("admin-a"),
        json={
            "expected_revision": active["revision"],
            "user_id": user_id,
            "enabled": True,
        },
    )
    assert response.status_code == 403
    assert repo.grants == old


def test_grant_cannot_assign_admin_role(setup):
    _, _, api = setup
    active = activate(api)
    response = api.put(
        f"{path(active)}/grant",
        headers=auth("admin-a"),
        json={
            "expected_revision": active["revision"],
            "user_id": "user-a",
            "enabled": True,
            "role": "platform_admin",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "mode,profile", [("SERVICE_ACCOUNT", None), ("USER_OAUTH", "dap_company_a")]
)
def test_auth_mode_requires_explicit_profile_contract(setup, mode, profile):
    _, _, api = setup
    definition = {
        **DEFINITION,
        "ga4": {**DEFINITION["ga4"], "auth_mode": mode},
        "data_access_profile_id": profile,
    }
    assert api.post(BASE, json=definition, headers=auth("admin-a")).status_code == 422


def test_sa_profile_company_and_validation_revision_are_checked(setup):
    repo, verifier, api = setup
    definition = {
        **DEFINITION,
        "ga4": {**DEFINITION["ga4"], "auth_mode": "SERVICE_ACCOUNT"},
        "data_access_profile_id": "dap_company_a",
    }
    draft = api.post(BASE, json=definition, headers=auth("admin-a")).json()
    verified = transition(api, draft, "verify").json()
    assert verifier.calls[0]["profile"].id == "dap_company_a"
    profile = repo.profiles["dap_company_a"]
    repo.profiles[profile.id] = profile.model_copy(
        update={"allowed_mcp_tools": frozenset()}
    )
    assert transition(api, verified, "activate").status_code == 409
    repo.profiles[profile.id] = profile.model_copy(update={"company_id": "company_b"})
    assert api.post(BASE, json=definition, headers=auth("admin-a")).status_code == 403


async def test_two_updates_with_same_revision_only_one_commits(setup):
    repo, verifier, api = setup
    draft = create(api)
    service = RegistrationService(repo, verifier)

    async def update():
        return await service.update(
            "admin-a",
            "company_a",
            draft["source_set"]["id"],
            RegistrationInput.model_validate(DEFINITION),
            1,
            "req-concurrent",
        )

    result = await asyncio.gather(update(), update(), return_exceptions=True)
    assert sum(isinstance(item, RegistrationError) for item in result) == 1
    assert (
        len([a for a in repo.audit_events if a.action == "admin.registration.update"])
        == 1
    )
