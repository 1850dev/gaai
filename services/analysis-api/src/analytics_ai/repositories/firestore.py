"""Firestore repository. Every lookup preserves explicit scope."""

from __future__ import annotations

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_document import DocumentSnapshot
from google.cloud.firestore_v1.base_query import FieldFilter

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


class FirestoreRepository:
    def __init__(self, project_id: str) -> None:
        self._client = firestore.AsyncClient(project=project_id)

    async def get_company(self, company_id: str) -> Company | None:
        snapshot = await self._client.collection("companies").document(company_id).get()
        return Company.model_validate(snapshot.to_dict()) if snapshot.exists else None

    async def list_company_sources(
        self, company_id: str, after: str = "", limit: int = 101
    ) -> list[DataSourceSet]:
        query = (
            self._client.collection("dataSourceSets")
            .where(filter=FieldFilter("company_id", "==", company_id))
            .order_by("__name__")
        )
        if after:
            query = query.where(
                filter=FieldFilter(
                    "__name__",
                    ">",
                    self._client.collection("dataSourceSets").document(after),
                )
            )
        return [
            DataSourceSet.model_validate(s.to_dict())
            async for s in query.limit(limit).stream()
        ]

    async def list_source_grants(
        self, source_id: str, limit: int = 101
    ) -> list[UserAccessGrant]:
        query = (
            self._client.collection("userAccessGrants")
            .where(filter=FieldFilter("data_source_set_id", "==", source_id))
            .limit(limit)
        )
        return [
            UserAccessGrant.model_validate(s.to_dict()) async for s in query.stream()
        ]

    async def get_registration(self, source_set_id: str) -> SourceRegistration | None:
        snapshot = (
            await self._client.collection("dataSourceRegistrations")
            .document(source_set_id)
            .get()
        )
        return (
            SourceRegistration.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def commit_registration(
        self,
        registration: SourceRegistration,
        *,
        expected_revision: int | None,
        audit: AuditEvent,
        grant: UserAccessGrant | None = None,
    ) -> None:
        source = registration.source_set
        registration_ref = self._client.collection("dataSourceRegistrations").document(
            source.id
        )
        source_ref = self._client.collection("dataSourceSets").document(source.id)
        company_ref = self._client.collection("companies").document(source.company_id)

        @firestore.async_transactional
        async def commit(transaction: firestore.AsyncTransaction) -> None:
            # All authorization dependencies are read IN the writing transaction.
            actor = (
                await self._client.collection("users")
                .document(audit.user_id)
                .get(transaction=transaction)
            )
            member = (
                await company_ref.collection("members")
                .document(audit.user_id)
                .get(transaction=transaction)
            )
            company = await company_ref.get(transaction=transaction)
            old = await registration_ref.get(transaction=transaction)
            published = await source_ref.get(transaction=transaction)
            profile = None
            if source.data_access_profile_id is not None:
                snapshot = (
                    await self._client.collection("dataAccessProfiles")
                    .document(source.data_access_profile_id)
                    .get(transaction=transaction)
                )
                profile = (
                    DataAccessProfile.model_validate(snapshot.to_dict())
                    if snapshot.exists
                    else None
                )
            recipient = None
            recipient_member = None
            if grant is not None:
                target = (
                    await self._client.collection("users")
                    .document(grant.user_id)
                    .get(transaction=transaction)
                )
                target_member = (
                    await company_ref.collection("members")
                    .document(grant.user_id)
                    .get(transaction=transaction)
                )
                recipient = (
                    User.model_validate(target.to_dict()) if target.exists else None
                )
                recipient_member = (
                    CompanyMember.model_validate(target_member.to_dict())
                    if target_member.exists
                    else None
                )
            previous = (
                SourceRegistration.model_validate(old.to_dict()) if old.exists else None
            )
            legacy_grants: list[DocumentSnapshot] = []
            if registration.legacy_source_hash and (
                previous is None
                or previous.status != "active"
                or registration.status == "disabled"
            ):
                legacy_grants = (
                    await self._client.collection("userAccessGrants")
                    .where(filter=FieldFilter("data_source_set_id", "==", source.id))
                    .limit(101)
                    .get(transaction=transaction)
                )
                if len(legacy_grants) > 100:
                    raise RegistrationError("registration_grant_inventory_too_large")
            check_write(
                actor=User.model_validate(actor.to_dict()) if actor.exists else None,
                member=CompanyMember.model_validate(member.to_dict())
                if member.exists
                else None,
                company=Company.model_validate(company.to_dict())
                if company.exists
                else None,
                profile=profile,
                previous=previous,
                current_source=DataSourceSet.model_validate(published.to_dict())
                if published.exists
                else None,
                updated=registration,
                expected_revision=expected_revision,
                grant=grant,
                recipient=recipient,
                recipient_member=recipient_member,
                source_grants=[
                    UserAccessGrant.model_validate(s.to_dict()) for s in legacy_grants
                ],
            )
            if grant is not None:
                existing = await (
                    self._client.collection("userAccessGrants")
                    .where(filter=FieldFilter("user_id", "==", grant.user_id))
                    .where(filter=FieldFilter("data_source_set_id", "==", source.id))
                    .where(filter=FieldFilter("enabled", "==", True))
                    .limit(2)
                    .get(transaction=transaction)
                )
                if len(existing) > 1 or any(item.id != grant.id for item in existing):
                    raise RegistrationError("registration_grant_conflict")
            transaction.set(registration_ref, registration.model_dump(mode="json"))
            migrating = (
                registration.status == "active"
                and previous is not None
                and previous.status != "active"
                and registration.legacy_source_hash
            )
            if migrating or registration.status == "disabled":
                transaction.set(source_ref, source.model_dump(mode="json"))
                for snapshot in legacy_grants:
                    old_grant = UserAccessGrant.model_validate(snapshot.to_dict())
                    transaction.set(
                        self._client.collection("userAccessGrants").document(
                            snapshot.id
                        ),
                        old_grant.model_copy(update={"enabled": False}).model_dump(
                            mode="json"
                        ),
                    )
            if (
                registration.status == "active"
                and previous is not None
                and previous.status != "active"
                and registration.legacy_source_hash is None
            ):
                transaction.create(source_ref, source.model_dump(mode="json"))
                for definition in Ga4ChatSessionService._semantic_definitions(source):
                    transaction.create(
                        self._client.collection("semanticDefinitions").document(
                            definition.id
                        ),
                        definition.model_dump(mode="json"),
                    )
            if grant is not None:
                transaction.set(
                    self._client.collection("userAccessGrants").document(grant.id),
                    grant.model_dump(mode="json"),
                )
            transaction.create(
                self._client.collection("auditEvents").document(audit.id),
                audit.model_dump(mode="json"),
            )

        await commit(self._client.transaction())

    async def get_user(self, user_id: str) -> User | None:
        snapshot = await self._client.collection("users").document(user_id).get()
        return User.model_validate(snapshot.to_dict()) if snapshot.exists else None

    async def get_company_member(
        self, company_id: str, user_id: str
    ) -> CompanyMember | None:
        snapshot = await (
            self._client.collection("companies")
            .document(company_id)
            .collection("members")
            .document(user_id)
            .get()
        )
        return (
            CompanyMember.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def list_company_members(self, user_id: str) -> list[CompanyMember]:
        query = self._client.collection_group("members").where(
            filter=FieldFilter("user_id", "==", user_id)
        )
        return [
            CompanyMember.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def get_data_access_profile(
        self, profile_id: str
    ) -> DataAccessProfile | None:
        snapshot = await (
            self._client.collection("dataAccessProfiles").document(profile_id).get()
        )
        return (
            DataAccessProfile.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def get_data_source_set(self, source_set_id: str) -> DataSourceSet | None:
        snapshot = await (
            self._client.collection("dataSourceSets").document(source_set_id).get()
        )
        return (
            DataSourceSet.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def list_user_access_grants(self, user_id: str) -> list[UserAccessGrant]:
        query = self._client.collection("userAccessGrants").where(
            filter=FieldFilter("user_id", "==", user_id)
        )
        return [
            UserAccessGrant.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def get_user_access_grant(
        self, user_id: str, source_set_id: str
    ) -> UserAccessGrant | None:
        query = (
            self._client.collection("userAccessGrants")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("data_source_set_id", "==", source_set_id))
            .where(filter=FieldFilter("enabled", "==", True))
            .limit(2)
        )
        grants = [
            UserAccessGrant.model_validate(s.to_dict()) async for s in query.stream()
        ]
        return grants[0] if len(grants) == 1 else None

    async def get_ga4_user_connection(self, user_id: str) -> Ga4UserConnection | None:
        snapshot = await (
            self._client.collection("ga4UserConnections").document(user_id).get()
        )
        return (
            Ga4UserConnection.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def put_ga4_user_connection(self, connection: Ga4UserConnection) -> None:
        await (
            self._client.collection("ga4UserConnections")
            .document(connection.user_id)
            .set(connection.model_dump(mode="json"))
        )

    async def create_ga4_oauth_transaction(
        self, transaction: Ga4OAuthTransaction
    ) -> None:
        payload = transaction.model_dump(mode="json")
        # Firestore TTL requires a native Timestamp rather than an ISO string.
        payload["expires_at"] = transaction.expires_at
        payload["created_at"] = transaction.created_at
        await (
            self._client.collection("ga4OAuthTransactions")
            .document(transaction.state_hash)
            .create(payload)
        )

    async def consume_ga4_oauth_transaction(
        self, state_hash: str
    ) -> Ga4OAuthTransaction | None:
        reference = self._client.collection("ga4OAuthTransactions").document(state_hash)

        @firestore.async_transactional
        async def consume(
            transaction: firestore.AsyncTransaction,
        ) -> Ga4OAuthTransaction | None:
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            oauth_transaction = Ga4OAuthTransaction.model_validate(snapshot.to_dict())
            transaction.delete(reference)
            return oauth_transaction

        return await consume(self._client.transaction())

    async def put_ga4_property_access(self, record: Ga4PropertyAccessRecord) -> None:
        document_id = f"{record.user_id}_{record.property_id}"
        await (
            self._client.collection("ga4PropertyAccess")
            .document(document_id)
            .set(record.model_dump(mode="json"))
        )

    async def create_session(self, session: AnalysisSession) -> None:
        await (
            self._client.collection("analysisSessions")
            .document(session.id)
            .create(session.model_dump(mode="json"))
        )

    async def get_session(self, session_id: str) -> AnalysisSession | None:
        snapshot = await (
            self._client.collection("analysisSessions").document(session_id).get()
        )
        return (
            AnalysisSession.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def list_sessions(self, user_id: str) -> list[AnalysisSession]:
        query = (
            self._client.collection("analysisSessions")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
        )
        return [
            AnalysisSession.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def update_session(
        self,
        session: AnalysisSession,
        *,
        expected_revision: int,
    ) -> bool:
        reference = self._client.collection("analysisSessions").document(session.id)

        @firestore.async_transactional
        async def replace(transaction: firestore.AsyncTransaction) -> bool:
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return False
            current = AnalysisSession.model_validate(snapshot.to_dict())
            if (
                current.revision != expected_revision
                or session.revision != expected_revision + 1
            ):
                return False
            transaction.set(reference, session.model_dump(mode="json"))
            return True

        return await replace(self._client.transaction())

    async def list_semantic_definitions(
        self,
        *,
        company_id: str,
        data_source_set_id: str,
        version: str,
    ) -> list[SemanticDefinition]:
        collection = self._client.collection("semanticDefinitions")
        queries = (
            collection.where(
                filter=FieldFilter("scope", "==", SemanticScope.GLOBAL.value)
            ).where(filter=FieldFilter("version", "==", version)),
            collection.where(
                filter=FieldFilter("scope", "==", SemanticScope.COMPANY.value)
            )
            .where(filter=FieldFilter("company_id", "==", company_id))
            .where(filter=FieldFilter("version", "==", version)),
            collection.where(
                filter=FieldFilter("scope", "==", SemanticScope.DATA_SOURCE_SET.value)
            )
            .where(filter=FieldFilter("company_id", "==", company_id))
            .where(filter=FieldFilter("data_source_set_id", "==", data_source_set_id))
            .where(filter=FieldFilter("version", "==", version)),
        )
        definitions: list[SemanticDefinition] = []
        for query in queries:
            async for snapshot in query.stream():
                definitions.append(
                    SemanticDefinition.model_validate(snapshot.to_dict())
                )
        return [item for item in definitions if item.enabled]

    async def create_message(self, message: AnalysisMessage) -> bool:
        try:
            await (
                self._client.collection("messages")
                .document(message.id)
                .create(message.model_dump(mode="json"))
            )
        except AlreadyExists:
            return False
        return True

    async def list_messages(self, session_id: str) -> list[AnalysisMessage]:
        query = (
            self._client.collection("messages")
            .where(filter=FieldFilter("session_id", "==", session_id))
            .order_by("created_at", direction=firestore.Query.ASCENDING)
        )
        return [
            AnalysisMessage.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def create_finding(self, finding: AnalysisFinding) -> None:
        await (
            self._client.collection("findings")
            .document(finding.id)
            .create(finding.model_dump(mode="json"))
        )

    async def get_finding(self, finding_id: str) -> AnalysisFinding | None:
        snapshot = await self._client.collection("findings").document(finding_id).get()
        return (
            AnalysisFinding.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def commit_analysis(
        self,
        session: AnalysisSession,
        assistant_message: AnalysisMessage,
        findings: tuple[AnalysisFinding, ...],
        *,
        expected_revision: int,
    ) -> bool:
        session_reference = self._client.collection("analysisSessions").document(
            session.id
        )
        message_reference = self._client.collection("messages").document(
            assistant_message.id
        )
        finding_references = tuple(
            self._client.collection("findings").document(finding.id)
            for finding in findings
        )

        @firestore.async_transactional
        async def commit(transaction: firestore.AsyncTransaction) -> bool:
            snapshot = await session_reference.get(transaction=transaction)
            if not snapshot.exists:
                return False
            current = AnalysisSession.model_validate(snapshot.to_dict())
            if (
                current.revision != expected_revision
                or session.revision != expected_revision + 1
            ):
                return False
            transaction.set(
                session_reference,
                session.model_dump(mode="json"),
            )
            for finding, finding_reference in zip(
                findings, finding_references, strict=True
            ):
                transaction.create(
                    finding_reference,
                    finding.model_dump(mode="json"),
                )
            transaction.create(
                message_reference,
                assistant_message.model_dump(mode="json"),
            )
            return True

        return await commit(self._client.transaction())

    async def get_latest_finding(self, session_id: str) -> AnalysisFinding | None:
        query = (
            self._client.collection("findings")
            .where(filter=FieldFilter("session_id", "==", session_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        async for snapshot in query.stream():
            return AnalysisFinding.model_validate(snapshot.to_dict())
        return None

    async def create_evidence(self, evidence: DatasetEvidence) -> None:
        await (
            self._client.collection("evidence")
            .document(evidence.id)
            .create(evidence.model_dump(mode="json"))
        )

    async def get_evidence(self, evidence_id: str) -> DatasetEvidence | None:
        snapshot = await self._client.collection("evidence").document(evidence_id).get()
        return (
            DatasetEvidence.model_validate(snapshot.to_dict())
            if snapshot.exists
            else None
        )

    async def write_audit_event(self, event: AuditEvent) -> None:
        await (
            self._client.collection("auditEvents")
            .document(event.id)
            .create(event.model_dump(mode="json"))
        )
