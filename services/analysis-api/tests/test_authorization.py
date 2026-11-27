from __future__ import annotations

import pytest

from analytics_ai.authorization.service import AccessDenied, AuthorizationService
from analytics_ai.repositories.memory import InMemoryRepository


@pytest.mark.asyncio
async def test_company_membership_without_grant_is_not_access(
    repository: InMemoryRepository,
) -> None:
    service = AuthorizationService(repository)
    with pytest.raises(AccessDenied) as denied:
        await service.authorize_source_set("user-a", "company_a_store")
    assert denied.value.internal_reason == "missing_source_set_grant"


@pytest.mark.asyncio
async def test_each_user_gets_independent_sets(
    repository: InMemoryRepository,
) -> None:
    service = AuthorizationService(repository)
    assert [item.id for item in await service.list_allowed_source_sets("user-a")] == [
        "company_a_web"
    ]
    assert [item.id for item in await service.list_allowed_source_sets("user-b")] == [
        "company_a_store"
    ]
