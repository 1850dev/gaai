"""S1a: discovery and company membership must not grant application data access."""

from copy import deepcopy

import pytest

from analytics_ai.domain.models import MemberRole, Status
from analytics_ai.ga4.chat_sessions import Ga4ChatSessionService
from analytics_ai.repositories.firestore import FirestoreRepository
from analytics_ai.repositories.memory import InMemoryRepository
from analytics_ai.repositories.protocols import ApplicationRepository
from tests.test_api_authorization import auth, client


def protected_state(repository):
    return deepcopy(
        (
            repository.source_sets,
            repository.grants,
            repository.semantic_definitions,
            repository.sessions,
        )
    )


@pytest.mark.parametrize("role", list(MemberRole))
def test_retired_property_endpoint_never_creates_scope_or_grant(repository, role):
    member = repository.members[("company_a", "user-a")]
    repository.members[("company_a", "user-a")] = member.model_copy(
        update={"role": role}
    )
    before = protected_state(repository)
    response = client(repository).post(
        "/api/ga4/sessions",
        headers=auth("user-a"),
        json={"property_id": "123456789", "enabled": True, "role": "platform_admin"},
    )
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "ga4_property_session_creation_retired"
    assert protected_state(repository) == before
    assert repository.audit_events[-1].action == "ga4.chat_session.retired"
    assert repository.audit_events[-1].status == "denied"


def test_retired_endpoint_still_requires_authentication(repository):
    before = protected_state(repository)
    assert (
        client(repository)
        .post("/api/ga4/sessions", json={"property_id": "123456789"})
        .status_code
        == 401
    )
    assert protected_state(repository) == before
    assert not repository.audit_events


def test_repository_and_service_have_no_self_grant_mutation():
    for implementation in (
        ApplicationRepository,
        InMemoryRepository,
        FirestoreRepository,
    ):
        assert not hasattr(implementation, "create_ga4_oauth_source_set")
    assert not hasattr(Ga4ChatSessionService, "create")


@pytest.mark.parametrize("target", ["123456789", "unregistered_set", "company_a_store"])
def test_property_or_ungranted_set_cannot_be_used_as_registered_set(repository, target):
    before = protected_state(repository)
    response = client(repository).post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": target},
    )
    assert response.status_code == 403
    assert protected_state(repository) == before


@pytest.mark.parametrize("revocation", ["grant", "source", "member", "user"])
def test_registered_set_is_reauthorized_before_session_creation(repository, revocation):
    if revocation == "grant":
        grant = repository.grants[("user-a", "company_a_web")]
        repository.grants[("user-a", "company_a_web")] = grant.model_copy(
            update={"enabled": False}
        )
    elif revocation == "source":
        source = repository.source_sets["company_a_web"]
        repository.source_sets[source.id] = source.model_copy(update={"enabled": False})
    elif revocation == "member":
        member = repository.members[("company_a", "user-a")]
        repository.members[("company_a", "user-a")] = member.model_copy(
            update={"status": Status.DISABLED}
        )
    else:
        user = repository.users["user-a"]
        repository.users["user-a"] = user.model_copy(update={"status": Status.DISABLED})
    before = protected_state(repository)
    response = client(repository).post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    )
    assert response.status_code == 403
    assert protected_state(repository) == before


def test_registered_session_preserves_grants_and_semantics(repository):
    before = protected_state(repository)[:3]
    response = client(repository).post(
        "/api/sessions",
        headers=auth("user-a"),
        json={"data_source_set_id": "company_a_web"},
    )
    assert response.status_code == 201
    assert protected_state(repository)[:3] == before
    assert response.json()["state"]["semantic_version"] == "a-web-v1"
    assert repository.audit_events[-1].action == "analysis_session.create"


def test_semantic_catalog_is_pure_and_retains_defaults(repository):
    before = protected_state(repository)
    definitions = Ga4ChatSessionService._semantic_definitions(
        repository.source_sets["company_a_web"]
    )
    by_name = {item.canonical_name: item for item in definitions}
    assert {
        "active_users",
        "page_path",
        "year_week",
        "year_month",
        "year",
        "mobile",
    } <= by_name.keys()
    assert by_name["organic_search"].ga4_mapping["value"] == "Organic Search"
    assert by_name["conversion"].available is False
    assert protected_state(repository) == before
