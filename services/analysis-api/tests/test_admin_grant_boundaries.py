"""Additional grant collision and company-path regression tests."""

import pytest

from analytics_ai.admin.models import GrantRequest, RegistrationError
from analytics_ai.domain.models import CompanyMember, MemberRole, Status
from tests.test_admin_firestore import prepare_active
from tests.test_admin_firestore import stored as firestore_fixture
from tests.test_admin_registration import activate, path
from tests.test_admin_registration import setup as registration_fixture
from tests.test_api_authorization import auth

setup = registration_fixture
stored = firestore_fixture


def test_conflicting_grant_is_not_implicitly_migrated(setup):
    repo, _, api = setup
    active = activate(api)
    source_id = active["source_set"]["id"]
    repo.grants[("user-a", source_id)] = repo.grants[
        ("user-a", "company_a_web")
    ].model_copy(update={"data_source_set_id": source_id})
    before = dict(repo.grants)
    response = api.put(
        f"{path(active)}/grant",
        headers=auth("admin-a"),
        json={
            "expected_revision": active["revision"],
            "user_id": "user-a",
            "enabled": False,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "registration_grant_conflict"
    assert repo.grants == before


def test_disabled_user_grant_can_still_be_revoked(setup):
    repo, _, api = setup
    active = activate(api)
    granted = api.put(
        f"{path(active)}/grant",
        headers=auth("admin-a"),
        json={
            "expected_revision": active["revision"],
            "user_id": "user-a",
            "enabled": True,
        },
    ).json()
    repo.users["user-a"] = repo.users["user-a"].model_copy(
        update={"status": Status.DISABLED}
    )
    response = api.put(
        f"{path(active)}/grant",
        headers=auth("admin-a"),
        json={
            "expected_revision": granted["revision"],
            "user_id": "user-a",
            "enabled": False,
        },
    )
    assert response.status_code == 200
    assert not repo.grants[("user-a", active["source_set"]["id"])].enabled


def test_source_cannot_be_read_or_mutated_through_another_company(setup):
    repo, verifier, api = setup
    active = activate(api)
    repo.members[("company_b", "admin-a")] = CompanyMember(
        company_id="company_b", user_id="admin-a", role=MemberRole.COMPANY_ADMIN
    )
    other = path(active).replace("company_a", "company_b")
    assert api.get(other, headers=auth("admin-a")).status_code == 404
    assert (
        api.post(
            other + "/verify",
            headers=auth("admin-a"),
            json={"expected_revision": active["revision"]},
        ).status_code
        == 404
    )
    assert len(verifier.calls) == 1


async def test_ambiguous_grants_abort_without_a_partial_write(stored):
    from copy import deepcopy

    client, _, service = stored
    active = await prepare_active(service)
    payload = {
        "user_id": "user-a",
        "data_source_set_id": active.source_set.id,
        "enabled": True,
        "role": "analyst",
    }
    client.data["userAccessGrants/old-one"] = {"id": "old-one", **payload}
    client.data["userAccessGrants/old-two"] = {"id": "old-two", **payload}
    before = deepcopy(client.data)
    with pytest.raises(RegistrationError, match="grant_conflict"):
        await service.grant(
            "admin-a",
            "company_a",
            active.source_set.id,
            GrantRequest(
                expected_revision=active.revision, user_id="user-a", enabled=False
            ),
            "req-grant",
        )
    assert client.data == before
    assert not client.transactions[-1].writes
