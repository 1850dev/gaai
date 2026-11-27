"""History and working memory must not survive a changed access scope as facts."""

from unittest.mock import AsyncMock

import pytest

from analytics_ai.admin.models import SourceRegistration
from analytics_ai.admin.policy import fingerprint
from analytics_ai.authorization.service import AccessDenied, AuthorizationService
from analytics_ai.domain.models import AnalysisSession, AnalysisState, SessionStatus
from analytics_ai.domain.scope import source_scope_fingerprint


def session(source, *, bound=False):
    return AnalysisSession(
        id="scope_history",
        user_id="user-a",
        company_id=source.company_id,
        data_source_set_id=source.id,
        data_access_profile_id=source.data_access_profile_id,
        source_scope_fingerprint=source_scope_fingerprint(source) if bound else None,
        state=AnalysisState(
            data_source_set_id=source.id, semantic_version=source.semantic_version
        ),
    )


@pytest.mark.asyncio
async def test_bound_session_rejects_changed_property_or_stream(repository):
    source = repository.source_sets["company_a_web"]
    original = session(source, bound=True)
    guard = AuthorizationService(repository)
    await guard.authorize_session("user-a", original)
    for ga4 in (
        source.ga4.model_copy(update={"property_id": "999999999"}),
        source.ga4.model_copy(update={"stream_scopes": ()}),
    ):
        repository.source_sets[source.id] = source.model_copy(update={"ga4": ga4})
        with pytest.raises(AccessDenied) as denied:
            await guard.authorize_session("user-a", original)
        assert denied.value.internal_reason == "session_source_scope_changed"


@pytest.mark.asyncio
async def test_legacy_scope_narrowing_keeps_records_but_denies_history(repository):
    current = repository.source_sets["company_a_web"]
    broad = current.model_copy(
        update={"ga4": current.ga4.model_copy(update={"stream_scopes": ()})}
    )
    original = session(broad)
    await repository.create_session(original)
    repository.registrations[current.id] = SourceRegistration(
        source_set=current,
        revision=4,
        status="active",
        legacy_source_hash=fingerprint(broad),
        legacy_grants_hash="previous-grants",
    )
    guard = AuthorizationService(repository)
    with pytest.raises(AccessDenied) as denied:
        await guard.authorize_session("user-a", original)
    assert denied.value.internal_reason == "legacy_session_source_scope_unverified"
    assert await repository.get_session(original.id) == original
    # A new session is explicitly bound, rather than restamping old results.
    await guard.authorize_session("user-a", session(current, bound=True))


@pytest.mark.asyncio
async def test_unchanged_legacy_web_registration_preserves_history(repository):
    current = repository.source_sets["company_a_web"]
    repository.registrations[current.id] = SourceRegistration(
        source_set=current,
        revision=4,
        status="active",
        legacy_source_hash=fingerprint(current),
        legacy_grants_hash="previous-grants",
    )
    await AuthorizationService(repository).authorize_session("user-a", session(current))


@pytest.mark.asyncio
async def test_bound_session_tolerates_set_name_change_not_resource_change(repository):
    current = repository.source_sets["company_a_web"]
    original = session(current, bound=True)
    repository.source_sets[current.id] = current.model_copy(update={"name": "New name"})
    await AuthorizationService(repository).authorize_session("user-a", original)


@pytest.mark.asyncio
async def test_session_listing_reuses_set_checks_only_within_one_request(
    repository, monkeypatch
):
    source = repository.source_sets["company_a_web"]
    get_grant = AsyncMock(wraps=repository.get_user_access_grant)
    get_registration = AsyncMock(wraps=repository.get_registration)
    monkeypatch.setattr(repository, "get_user_access_grant", get_grant)
    monkeypatch.setattr(repository, "get_registration", get_registration)
    for index in range(12):
        item = session(source, bound=index % 2 == 0)
        await repository.create_session(item.model_copy(update={"id": f"s-{index}"}))

    guard = AuthorizationService(repository)
    expected = await repository.list_sessions("user-a")
    assert await guard.list_allowed_sessions("user-a") == expected
    get_grant.assert_awaited_once_with("user-a", source.id)
    get_registration.assert_awaited_once_with(source.id)

    # A subsequent request sees revocation even on the same service instance.
    del repository.grants[("user-a", source.id)]
    assert await guard.list_allowed_sessions("user-a") == []
    assert get_grant.await_count == 2
    assert get_registration.await_count == 1


@pytest.mark.asyncio
async def test_session_listing_still_filters_each_session_binding(repository):
    source = repository.source_sets["company_a_web"]
    store = repository.source_sets["company_a_store"]
    company_a_web = session(source, bound=True)
    repository.registrations[source.id] = SourceRegistration(
        source_set=source,
        revision=4,
        status="active",
        legacy_source_hash="prior-broader-scope",
        legacy_grants_hash="previous-grants",
    )
    for item in (
        company_a_web,
        company_a_web.model_copy(
            update={"id": "changed", "source_scope_fingerprint": "prior-bound-scope"}
        ),
        company_a_web.model_copy(
            update={"id": "archived", "status": SessionStatus.ARCHIVED}
        ),
        session(source).model_copy(update={"id": "legacy"}),
        session(store, bound=True).model_copy(update={"id": "denied-set"}),
    ):
        await repository.create_session(item)
    assert await AuthorizationService(repository).list_allowed_sessions("user-a") == [
        company_a_web
    ]
