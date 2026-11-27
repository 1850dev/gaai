"""Authorization service producing server-trusted scopes."""

from __future__ import annotations

from dataclasses import dataclass

from analytics_ai.domain.models import (
    AnalysisSession,
    CompanyMember,
    DataAccessProfile,
    DataSourceSet,
    SessionStatus,
    Status,
    User,
    UserAccessGrant,
)
from analytics_ai.domain.scope import (
    published_source_fingerprint,
    source_scope_fingerprint,
)
from analytics_ai.repositories.protocols import ApplicationRepository


class AccessDenied(Exception):
    """Generic denial; details are for protected audit logs only."""

    def __init__(self, internal_reason: str) -> None:
        super().__init__("access denied")
        self.internal_reason = internal_reason


@dataclass(frozen=True, slots=True)
class AuthorizedScope:
    user: User
    member: CompanyMember
    grant: UserAccessGrant
    source_set: DataSourceSet
    profile: DataAccessProfile | None


class AuthorizationService:
    def __init__(self, repository: ApplicationRepository) -> None:
        self._repository = repository

    async def authorize_source_set(
        self, user_id: str, source_set_id: str
    ) -> AuthorizedScope:
        user = await self._repository.get_user(user_id)
        if user is None or user.status is not Status.ACTIVE:
            raise AccessDenied("inactive_or_unregistered_user")

        grant = await self._repository.get_user_access_grant(user_id, source_set_id)
        if grant is None or not grant.enabled:
            raise AccessDenied("missing_source_set_grant")

        source_set = await self._repository.get_data_source_set(source_set_id)
        if source_set is None or not source_set.enabled:
            raise AccessDenied("source_set_unavailable")

        member = await self._repository.get_company_member(
            source_set.company_id, user_id
        )
        if member is None or member.status is not Status.ACTIVE:
            raise AccessDenied("inactive_company_membership")

        profile = None
        if source_set.data_access_profile_id is not None:
            profile = await self._repository.get_data_access_profile(
                source_set.data_access_profile_id
            )
        if profile is not None and profile.company_id != source_set.company_id:
            raise AccessDenied("profile_company_mismatch")

        return AuthorizedScope(
            user=user,
            member=member,
            grant=grant,
            source_set=source_set,
            profile=profile,
        )

    async def authorize_session(
        self,
        user_id: str,
        session: AnalysisSession,
        *,
        proposed_source_set_id: str | None = None,
    ) -> AuthorizedScope:
        self._check_session_identity(user_id, session, proposed_source_set_id)
        scope = await self.authorize_source_set(user_id, session.data_source_set_id)
        self._check_session_scope(session, scope)
        if session.source_scope_fingerprint is None:
            legacy_hash = await self._legacy_source_hash(session.data_source_set_id)
            self._check_legacy_scope(scope, legacy_hash)
        return scope

    @staticmethod
    def _check_session_identity(
        user_id: str,
        session: AnalysisSession,
        proposed_source_set_id: str | None = None,
    ) -> None:
        if session.user_id != user_id:
            raise AccessDenied("session_owner_mismatch")
        if session.status is not SessionStatus.ACTIVE:
            raise AccessDenied("session_inactive")
        if (
            proposed_source_set_id is not None
            and proposed_source_set_id != session.data_source_set_id
        ):
            raise AccessDenied("session_scope_change_attempt")

    @staticmethod
    def _check_session_scope(session: AnalysisSession, scope: AuthorizedScope) -> None:
        if scope.source_set.data_access_profile_id != session.data_access_profile_id:
            raise AccessDenied("session_profile_changed")
        if scope.source_set.company_id != session.company_id:
            raise AccessDenied("session_company_changed")
        if session.source_scope_fingerprint is not None:
            if session.source_scope_fingerprint != source_scope_fingerprint(
                scope.source_set
            ):
                raise AccessDenied("session_source_scope_changed")

    async def _legacy_source_hash(self, source_set_id: str) -> str | None:
        registration = await self._repository.get_registration(source_set_id)
        return registration.legacy_source_hash if registration is not None else None

    @staticmethod
    def _check_legacy_scope(scope: AuthorizedScope, legacy_hash: str | None) -> None:
        if legacy_hash is not None and legacy_hash != published_source_fingerprint(
            scope.source_set
        ):
            # Never release legacy results under a changed registration baseline.
            raise AccessDenied("legacy_session_source_scope_unverified")

    async def list_allowed_sessions(self, user_id: str) -> list[AnalysisSession]:
        sessions = await self._repository.list_sessions(user_id)
        allowed: list[AnalysisSession] = []
        # Only share per-Set reads inside this listing request. Session scope
        # bindings are checked for every row, and detail routes authorize afresh.
        scopes: dict[str, AuthorizedScope | None] = {}
        legacy_hashes: dict[str, str | None] = {}
        for session in sessions:
            source_set_id = session.data_source_set_id
            try:
                self._check_session_identity(user_id, session)
                if source_set_id not in scopes:
                    try:
                        scopes[source_set_id] = await self.authorize_source_set(
                            user_id, source_set_id
                        )
                    except AccessDenied:
                        scopes[source_set_id] = None
                scope = scopes[source_set_id]
                if scope is None:
                    continue
                self._check_session_scope(session, scope)
                if session.source_scope_fingerprint is None:
                    if source_set_id not in legacy_hashes:
                        legacy_hashes[source_set_id] = await self._legacy_source_hash(
                            source_set_id
                        )
                    self._check_legacy_scope(scope, legacy_hashes[source_set_id])
            except AccessDenied:
                continue
            allowed.append(session)
        return allowed

    async def list_allowed_source_sets(self, user_id: str) -> list[DataSourceSet]:
        user = await self._repository.get_user(user_id)
        if user is None or user.status is not Status.ACTIVE:
            raise AccessDenied("inactive_or_unregistered_user")
        grants = await self._repository.list_user_access_grants(user_id)
        result: list[DataSourceSet] = []
        for grant in grants:
            if not grant.enabled:
                continue
            try:
                scope = await self.authorize_source_set(
                    user_id, grant.data_source_set_id
                )
            except AccessDenied:
                continue
            result.append(scope.source_set)
        return sorted(result, key=lambda item: (item.company_id, item.name))
