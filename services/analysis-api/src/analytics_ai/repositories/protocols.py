"""Repository protocol that keeps authorization storage replaceable."""

from __future__ import annotations

from typing import Protocol

from analytics_ai.admin.models import SourceRegistration
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
    User,
    UserAccessGrant,
)


class ApplicationRepository(Protocol):
    async def list_company_sources(
        self, company_id: str, after: str = "", limit: int = 101
    ) -> list[DataSourceSet]: ...

    async def list_source_grants(
        self, source_id: str, limit: int = 101
    ) -> list[UserAccessGrant]: ...

    async def get_company(self, company_id: str) -> Company | None: ...

    async def get_registration(
        self, source_set_id: str
    ) -> SourceRegistration | None: ...

    async def commit_registration(
        self,
        registration: SourceRegistration,
        *,
        expected_revision: int | None,
        audit: AuditEvent,
        grant: UserAccessGrant | None = None,
    ) -> None: ...

    async def get_user(self, user_id: str) -> User | None: ...

    async def get_company_member(
        self, company_id: str, user_id: str
    ) -> CompanyMember | None: ...

    async def list_company_members(self, user_id: str) -> list[CompanyMember]: ...

    async def get_data_access_profile(
        self, profile_id: str
    ) -> DataAccessProfile | None: ...

    async def get_data_source_set(self, source_set_id: str) -> DataSourceSet | None: ...

    async def list_user_access_grants(self, user_id: str) -> list[UserAccessGrant]: ...

    async def get_user_access_grant(
        self, user_id: str, source_set_id: str
    ) -> UserAccessGrant | None: ...

    async def get_ga4_user_connection(
        self, user_id: str
    ) -> Ga4UserConnection | None: ...

    async def put_ga4_user_connection(self, connection: Ga4UserConnection) -> None: ...

    async def create_ga4_oauth_transaction(
        self, transaction: Ga4OAuthTransaction
    ) -> None: ...

    async def consume_ga4_oauth_transaction(
        self, state_hash: str
    ) -> Ga4OAuthTransaction | None: ...

    async def put_ga4_property_access(
        self, record: Ga4PropertyAccessRecord
    ) -> None: ...

    async def create_session(self, session: AnalysisSession) -> None: ...

    async def get_session(self, session_id: str) -> AnalysisSession | None: ...

    async def list_sessions(self, user_id: str) -> list[AnalysisSession]: ...

    async def update_session(
        self,
        session: AnalysisSession,
        *,
        expected_revision: int,
    ) -> bool: ...

    async def list_semantic_definitions(
        self,
        *,
        company_id: str,
        data_source_set_id: str,
        version: str,
    ) -> list[SemanticDefinition]: ...

    async def create_message(self, message: AnalysisMessage) -> bool: ...

    async def list_messages(self, session_id: str) -> list[AnalysisMessage]: ...

    async def create_finding(self, finding: AnalysisFinding) -> None: ...

    async def get_finding(self, finding_id: str) -> AnalysisFinding | None: ...

    async def commit_analysis(
        self,
        session: AnalysisSession,
        assistant_message: AnalysisMessage,
        findings: tuple[AnalysisFinding, ...],
        *,
        expected_revision: int,
    ) -> bool: ...

    async def get_latest_finding(self, session_id: str) -> AnalysisFinding | None: ...

    async def create_evidence(self, evidence: DatasetEvidence) -> None: ...

    async def get_evidence(self, evidence_id: str) -> DatasetEvidence | None: ...

    async def write_audit_event(self, event: AuditEvent) -> None: ...
