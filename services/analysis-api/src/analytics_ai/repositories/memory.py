"""In-memory repository for unit tests and isolated local development."""

from __future__ import annotations

import asyncio

from analytics_ai.admin.models import RegistrationError, SourceRegistration
from analytics_ai.admin.policy import check_write
from analytics_ai.domain.models import (
    AnalysisFinding,
    AnalysisMessage,
    AnalysisSession,
    AuditEvent,
    Company,
    CompanyMember,
    DataAccessProfile,
    DatasetEvidence,
    DataSourceSet,
    Ga4OAuthTransaction,
    Ga4PropertyAccessRecord,
    Ga4UserConnection,
    SemanticDefinition,
    SemanticScope,
    User,
    UserAccessGrant,
)
from analytics_ai.ga4.chat_sessions import Ga4ChatSessionService


class InMemoryRepository:
    def __init__(
        self,
        *,
        users: list[User] | None = None,
        companies: list[Company] | None = None,
        members: list[CompanyMember] | None = None,
        profiles: list[DataAccessProfile] | None = None,
        source_sets: list[DataSourceSet] | None = None,
        grants: list[UserAccessGrant] | None = None,
        semantic_definitions: list[SemanticDefinition] | None = None,
    ) -> None:
        self.users = {item.uid: item for item in users or []}
        self.companies = {item.id: item for item in companies or []}
        self.registrations: dict[str, SourceRegistration] = {}
        self.members = {(item.company_id, item.user_id): item for item in members or []}
        self.profiles = {item.id: item for item in profiles or []}
        self.source_sets = {item.id: item for item in source_sets or []}
        self.grants = {
            (item.user_id, item.data_source_set_id): item for item in grants or []
        }
        self.sessions: dict[str, AnalysisSession] = {}
        self.semantic_definitions = {
            item.id: item for item in semantic_definitions or []
        }
        self.messages: dict[str, AnalysisMessage] = {}
        self.findings: dict[str, AnalysisFinding] = {}
        self.evidence: dict[str, DatasetEvidence] = {}
        self.ga4_user_connections: dict[str, Ga4UserConnection] = {}
        self.ga4_oauth_transactions: dict[str, Ga4OAuthTransaction] = {}
        self.ga4_property_access: dict[tuple[str, str], Ga4PropertyAccessRecord] = {}
        self.audit_events: list[AuditEvent] = []
        self._lock = asyncio.Lock()

    async def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    async def get_company(self, company_id: str) -> Company | None:
        return self.companies.get(company_id)

    async def list_company_sources(
        self, company_id: str, after: str = "", limit: int = 101
    ) -> list[DataSourceSet]:
        return sorted(
            (
                s
                for s in self.source_sets.values()
                if s.company_id == company_id and s.id > after
            ),
            key=lambda s: s.id,
        )[:limit]

    async def list_source_grants(
        self, source_id: str, limit: int = 101
    ) -> list[UserAccessGrant]:
        return sorted(
            (g for g in self.grants.values() if g.data_source_set_id == source_id),
            key=lambda g: g.id,
        )[:limit]

    async def get_registration(self, source_set_id: str) -> SourceRegistration | None:
        return self.registrations.get(source_set_id)

    async def commit_registration(
        self,
        registration: SourceRegistration,
        *,
        expected_revision: int | None,
        audit: AuditEvent,
        grant: UserAccessGrant | None = None,
    ) -> None:
        async with self._lock:
            source = registration.source_set
            previous = self.registrations.get(source.id)
            source_grants = await self.list_source_grants(source.id)
            if len(source_grants) > 100 and registration.legacy_source_hash:
                raise RegistrationError("registration_grant_inventory_too_large")
            check_write(
                actor=self.users.get(audit.user_id),
                member=self.members.get((source.company_id, audit.user_id)),
                company=self.companies.get(source.company_id),
                profile=self.profiles.get(source.data_access_profile_id or ""),
                previous=previous,
                current_source=self.source_sets.get(source.id),
                updated=registration,
                expected_revision=expected_revision,
                grant=grant,
                recipient=self.users.get(grant.user_id) if grant else None,
                recipient_member=self.members.get((source.company_id, grant.user_id))
                if grant
                else None,
                source_grants=source_grants,
            )
            existing_grant = (
                self.grants.get((grant.user_id, source.id)) if grant else None
            )
            if (
                grant
                and existing_grant
                and existing_grant.enabled
                and existing_grant.id != grant.id
            ):
                raise RegistrationError("registration_grant_conflict")
            definitions = (
                Ga4ChatSessionService._semantic_definitions(source)
                if registration.status == "active"
                and previous is not None
                and previous.status != "active"
                and registration.legacy_source_hash is None
                else []
            )
            self.registrations[source.id] = registration
            if registration.status == "disabled" or (
                registration.status == "active"
                and previous is not None
                and previous.status != "active"
                and registration.legacy_source_hash
            ):
                self.source_sets[source.id] = source
                for old_grant in source_grants:
                    self.grants[(old_grant.user_id, source.id)] = old_grant.model_copy(
                        update={"enabled": False}
                    )
            if definitions:
                self.source_sets[source.id] = source
                self.semantic_definitions.update(
                    {item.id: item for item in definitions}
                )
            if grant is not None:
                self.grants[(grant.user_id, source.id)] = grant
            self.audit_events.append(audit)

    async def get_company_member(
        self, company_id: str, user_id: str
    ) -> CompanyMember | None:
        return self.members.get((company_id, user_id))

    async def list_company_members(self, user_id: str) -> list[CompanyMember]:
        return sorted(
            (
                member
                for (_, member_user_id), member in self.members.items()
                if member_user_id == user_id
            ),
            key=lambda member: member.company_id,
        )

    async def get_data_access_profile(
        self, profile_id: str
    ) -> DataAccessProfile | None:
        return self.profiles.get(profile_id)

    async def get_data_source_set(self, source_set_id: str) -> DataSourceSet | None:
        return self.source_sets.get(source_set_id)

    async def list_user_access_grants(self, user_id: str) -> list[UserAccessGrant]:
        return [item for (uid, _), item in self.grants.items() if uid == user_id]

    async def get_user_access_grant(
        self, user_id: str, source_set_id: str
    ) -> UserAccessGrant | None:
        return self.grants.get((user_id, source_set_id))

    async def get_ga4_user_connection(self, user_id: str) -> Ga4UserConnection | None:
        return self.ga4_user_connections.get(user_id)

    async def put_ga4_user_connection(self, connection: Ga4UserConnection) -> None:
        async with self._lock:
            self.ga4_user_connections[connection.user_id] = connection

    async def create_ga4_oauth_transaction(
        self, transaction: Ga4OAuthTransaction
    ) -> None:
        async with self._lock:
            if transaction.state_hash in self.ga4_oauth_transactions:
                raise ValueError("GA4 OAuth transaction already exists")
            self.ga4_oauth_transactions[transaction.state_hash] = transaction

    async def consume_ga4_oauth_transaction(
        self, state_hash: str
    ) -> Ga4OAuthTransaction | None:
        async with self._lock:
            return self.ga4_oauth_transactions.pop(state_hash, None)

    async def put_ga4_property_access(self, record: Ga4PropertyAccessRecord) -> None:
        async with self._lock:
            self.ga4_property_access[(record.user_id, record.property_id)] = record

    async def create_session(self, session: AnalysisSession) -> None:
        async with self._lock:
            if session.id in self.sessions:
                raise ValueError("session already exists")
            self.sessions[session.id] = session

    async def get_session(self, session_id: str) -> AnalysisSession | None:
        return self.sessions.get(session_id)

    async def list_sessions(self, user_id: str) -> list[AnalysisSession]:
        sessions = [s for s in self.sessions.values() if s.user_id == user_id]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    async def update_session(
        self,
        session: AnalysisSession,
        *,
        expected_revision: int,
    ) -> bool:
        async with self._lock:
            current = self.sessions.get(session.id)
            if (
                current is None
                or current.revision != expected_revision
                or session.revision != expected_revision + 1
            ):
                return False
            self.sessions[session.id] = session
            return True

    async def list_semantic_definitions(
        self,
        *,
        company_id: str,
        data_source_set_id: str,
        version: str,
    ) -> list[SemanticDefinition]:
        return [
            item
            for item in self.semantic_definitions.values()
            if item.enabled
            and item.version == version
            and (
                item.scope is SemanticScope.GLOBAL
                or (
                    item.scope is SemanticScope.COMPANY
                    and item.company_id == company_id
                )
                or (
                    item.scope is SemanticScope.DATA_SOURCE_SET
                    and item.company_id == company_id
                    and item.data_source_set_id == data_source_set_id
                )
            )
        ]

    async def create_message(self, message: AnalysisMessage) -> bool:
        async with self._lock:
            if message.id in self.messages:
                return False
            self.messages[message.id] = message
            return True

    async def list_messages(self, session_id: str) -> list[AnalysisMessage]:
        messages = [
            item for item in self.messages.values() if item.session_id == session_id
        ]
        return sorted(messages, key=lambda item: (item.created_at, item.id))

    async def create_finding(self, finding: AnalysisFinding) -> None:
        async with self._lock:
            if finding.id in self.findings:
                raise ValueError("finding already exists")
            self.findings[finding.id] = finding

    async def get_finding(self, finding_id: str) -> AnalysisFinding | None:
        return self.findings.get(finding_id)

    async def commit_analysis(
        self,
        session: AnalysisSession,
        assistant_message: AnalysisMessage,
        findings: tuple[AnalysisFinding, ...],
        *,
        expected_revision: int,
    ) -> bool:
        async with self._lock:
            current = self.sessions.get(session.id)
            if (
                current is None
                or current.revision != expected_revision
                or session.revision != expected_revision + 1
                or assistant_message.id in self.messages
                or any(finding.id in self.findings for finding in findings)
            ):
                return False
            self.sessions[session.id] = session
            for finding in findings:
                self.findings[finding.id] = finding
            self.messages[assistant_message.id] = assistant_message
            return True

    async def get_latest_finding(self, session_id: str) -> AnalysisFinding | None:
        findings = [
            item for item in self.findings.values() if item.session_id == session_id
        ]
        return max(findings, key=lambda item: item.created_at, default=None)

    async def create_evidence(self, evidence: DatasetEvidence) -> None:
        async with self._lock:
            if evidence.id in self.evidence:
                raise ValueError("evidence already exists")
            self.evidence[evidence.id] = evidence

    async def get_evidence(self, evidence_id: str) -> DatasetEvidence | None:
        return self.evidence.get(evidence_id)

    async def write_audit_event(self, event: AuditEvent) -> None:
        async with self._lock:
            self.audit_events.append(event)
