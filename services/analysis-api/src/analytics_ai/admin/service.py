"""Explicit draft → verify → activate, followed by a separate grant operation."""

import asyncio
import hashlib
import json
import re
import uuid

from google.api_core.exceptions import Forbidden

from analytics_ai.admin.models import (
    DisableRegistrationRequest,
    GrantRequest,
    LegacyRegistrationRequest,
    RegistrationError,
    RegistrationInput,
    RegistrationProof,
    SourceRegistration,
    VerificationResult,
)
from analytics_ai.admin.policy import (
    fingerprint,
    grants_fingerprint,
    require_admin,
    require_profile,
)
from analytics_ai.admin.verification import RegistrationVerifier
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import (
    AuditEvent,
    DataSourceSet,
    UserAccessGrant,
    utc_now,
)
from analytics_ai.ga4.chat_sessions import SEMANTIC_VERSION
from analytics_ai.ga4.credentials import Ga4CredentialError
from analytics_ai.ga4.verification import Ga4VerificationError
from analytics_ai.repositories.protocols import ApplicationRepository


class RegistrationService:
    def __init__(
        self, repository: ApplicationRepository, verifier: RegistrationVerifier
    ) -> None:
        self.repository = repository
        self.verifier = verifier

    async def authorize(self, user_id: str, company_id: str) -> None:
        require_admin(
            await self.repository.get_user(user_id),
            await self.repository.get_company_member(company_id, user_id),
            await self.repository.get_company(company_id),
            company_id,
        )

    async def companies(self, user_id: str) -> list[dict[str, str]]:
        result = []
        for member in await self.repository.list_company_members(user_id):
            try:
                await self.authorize(user_id, member.company_id)
            except AccessDenied:
                continue
            company = await self.repository.get_company(member.company_id)
            if company:
                result.append({"id": company.id, "name": company.name})
        return result

    async def inventory(
        self, user_id: str, company_id: str, after: str = ""
    ) -> dict[str, object]:
        await self.authorize(user_id, company_id)
        if after and not re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", after):
            raise RegistrationError("registration_invalid_cursor", 422)
        sources = await self.repository.list_company_sources(company_id, after)
        items = []
        for source in sources[:100]:
            grants = await self.repository.list_source_grants(source.id)
            registration = await self.repository.get_registration(source.id)
            items.append(
                {
                    "source_set": source.model_dump(mode="json"),
                    "registration": registration.model_dump(mode="json")
                    if registration
                    else None,
                    "source_hash": fingerprint(source),
                    "grants_hash": grants_fingerprint(grants)
                    if len(grants) <= 100
                    else None,
                    "grants": [g.model_dump(mode="json") for g in grants[:100]],
                    "grants_truncated": len(grants) > 100,
                }
            )
        return {
            "items": items,
            "next_cursor": sources[99].id if len(sources) > 100 else None,
        }

    async def legacy_source(
        self, user_id: str, company_id: str, source_id: str
    ) -> DataSourceSet:
        await self.authorize(user_id, company_id)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", source_id):
            raise RegistrationError("registration_not_found", 404)
        source = await self.repository.get_data_source_set(source_id)
        if source is None or source.company_id != company_id:
            raise RegistrationError("registration_not_found", 404)
        return source

    async def stage_legacy(
        self,
        user_id: str,
        company_id: str,
        source_id: str,
        payload: LegacyRegistrationRequest,
        request_id: str,
    ) -> SourceRegistration:
        old = await self.legacy_source(user_id, company_id, source_id)
        source = self._source(source_id, company_id, payload.definition).model_copy(
            update={"semantic_version": old.semantic_version}
        )
        record = SourceRegistration(
            source_set=source,
            revision=1,
            legacy_source_hash=payload.source_hash,
            legacy_grants_hash=payload.grants_hash,
        )
        await self._commit(record, user_id, request_id, "legacy_staged", None)
        return record

    async def disable(
        self,
        user_id: str,
        company_id: str,
        source_id: str,
        payload: DisableRegistrationRequest,
        request_id: str,
    ) -> SourceRegistration:
        old = await self.legacy_source(user_id, company_id, source_id)
        record = SourceRegistration(
            source_set=old.model_copy(update={"enabled": False}),
            revision=(payload.expected_revision or 0) + 1,
            status="disabled",
            legacy_source_hash=payload.source_hash,
            legacy_grants_hash=payload.grants_hash,
        )
        await self._commit(
            record, user_id, request_id, "disabled", payload.expected_revision
        )
        return record

    async def read(
        self, user_id: str, company_id: str, source_id: str
    ) -> SourceRegistration:
        await self.authorize(user_id, company_id)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", source_id):
            raise RegistrationError("registration_not_found", 404)
        record = await self.repository.get_registration(source_id)
        if record is None or record.source_set.company_id != company_id:
            raise RegistrationError("registration_not_found", 404)
        return record

    async def create(
        self,
        user_id: str,
        company_id: str,
        definition: RegistrationInput,
        request_id: str,
    ) -> SourceRegistration:
        await self.authorize(user_id, company_id)
        source = self._source(f"dss_{uuid.uuid4().hex}", company_id, definition)
        record = SourceRegistration(source_set=source, revision=1)
        await self._commit(record, user_id, request_id, "create", None)
        return record

    @staticmethod
    def _source(
        source_id: str, company_id: str, definition: RegistrationInput
    ) -> DataSourceSet:
        return DataSourceSet(
            id=source_id,
            company_id=company_id,
            name=definition.name,
            ga4=definition.ga4,
            data_access_profile_id=definition.data_access_profile_id,
            semantic_version=SEMANTIC_VERSION,
            enabled=False,
        )

    async def update(
        self,
        user_id: str,
        company_id: str,
        source_id: str,
        definition: RegistrationInput,
        revision: int,
        request_id: str,
    ) -> SourceRegistration:
        record = await self.read(user_id, company_id, source_id)
        self._pending(record, revision)
        updated = record.model_copy(
            update={
                "source_set": self._source(
                    source_id, company_id, definition
                ).model_copy(
                    update={"semantic_version": record.source_set.semantic_version}
                ),
                "revision": revision + 1,
                "status": "draft",
                "proof": None,
            }
        )
        await self._commit(updated, user_id, request_id, "update", revision)
        return updated

    @staticmethod
    def _pending(record: SourceRegistration, revision: int) -> None:
        if record.revision != revision:
            raise RegistrationError("registration_revision_conflict")
        if record.status not in {"draft", "verified"}:
            raise RegistrationError("registration_active_immutable")

    async def verify(
        self,
        user_id: str,
        company_id: str,
        source_id: str,
        revision: int,
        request_id: str,
    ) -> SourceRegistration:
        record = await self.read(user_id, company_id, source_id)
        self._pending(record, revision)
        # Clear any old success BEFORE I/O. Failure/crash cannot leave usable proof.
        pending = record.model_copy(
            update={"revision": revision + 1, "status": "draft", "proof": None}
        )
        await self._commit(
            pending, user_id, request_id, "verification_started", revision
        )
        source = pending.source_set
        profile = (
            await self.repository.get_data_access_profile(source.data_access_profile_id)
            if source.data_access_profile_id
            else None
        )
        require_profile(source, profile)
        try:
            async with asyncio.timeout(45):
                verification = await self.verifier.verify(
                    user_id=user_id, source_set=source, profile=profile
                )
        except Forbidden as error:
            raise RegistrationError("ga4_property_access_denied", 403) from error
        except (Ga4CredentialError, Ga4VerificationError) as error:
            raise RegistrationError(error.code, 422) from error
        except RegistrationError:
            raise
        except Exception as error:
            # Never expose raw upstream messages, credentials or property metadata.
            raise RegistrationError("registration_verification_failed", 503) from error
        proof = RegistrationProof(
            source_hash=fingerprint(source),
            verified_by=user_id,
            verified_at=utc_now(),
            credential_identity=verification.credential_identity
            if isinstance(verification, VerificationResult)
            else verification,
            profile_hash=fingerprint(profile) if profile else None,
            stream_metadata_parent=verification.stream_metadata_parent
            if isinstance(verification, VerificationResult)
            else None,
        )
        verified = pending.model_copy(
            update={
                "revision": pending.revision + 1,
                "status": "verified",
                "proof": proof,
            }
        )
        await self._commit(verified, user_id, request_id, "verified", pending.revision)
        return verified

    async def activate(
        self,
        user_id: str,
        company_id: str,
        source_id: str,
        revision: int,
        request_id: str,
    ) -> SourceRegistration:
        record = await self.read(user_id, company_id, source_id)
        self._pending(record, revision)
        updated = record.model_copy(
            update={
                "revision": revision + 1,
                "status": "active",
                "source_set": record.source_set.model_copy(update={"enabled": True}),
            }
        )
        await self._commit(updated, user_id, request_id, "activated", revision)
        return updated

    async def grant(
        self,
        user_id: str,
        company_id: str,
        source_id: str,
        payload: GrantRequest,
        request_id: str,
    ) -> SourceRegistration:
        record = await self.read(user_id, company_id, source_id)
        if record.revision != payload.expected_revision:
            raise RegistrationError("registration_revision_conflict")
        identifier = hashlib.sha256(
            json.dumps([payload.user_id, source_id]).encode()
        ).hexdigest()
        grant = UserAccessGrant(
            id=f"managed_{identifier}",
            user_id=payload.user_id,
            data_source_set_id=source_id,
            enabled=payload.enabled,
        )
        updated = record.model_copy(update={"revision": record.revision + 1})
        await self._commit(
            updated, user_id, request_id, "grant_changed", record.revision, grant
        )
        return updated

    async def _commit(
        self,
        record: SourceRegistration,
        user_id: str,
        request_id: str,
        action: str,
        expected_revision: int | None,
        grant: UserAccessGrant | None = None,
    ) -> None:
        details: dict[str, object] = {
            "revision": record.revision,
            "state": record.status,
            "source_hash": fingerprint(record.source_set),
        }
        if record.legacy_source_hash:
            details.update(
                {
                    "legacy_source_hash": record.legacy_source_hash,
                    "reviewed_grants_hash": record.legacy_grants_hash,
                    "legacy_grants_disabled": action in {"activated", "disabled"},
                }
            )
        if record.proof:
            details["verified_by"] = record.proof.verified_by
            details["credential_identity"] = record.proof.credential_identity
            details["stream_metadata_parent"] = record.proof.stream_metadata_parent
        if record.source_set.ga4:
            details["ga4_property_id"] = record.source_set.ga4.property_id
            details["ga4_auth_mode"] = record.source_set.ga4.auth_mode.value
            details["stream_scopes"] = [
                scope.model_dump(mode="json")
                for scope in record.source_set.ga4.stream_scopes
            ]
        if grant:
            details.update(
                {"recipient_uid": grant.user_id, "grant_enabled": grant.enabled}
            )
        await self.repository.commit_registration(
            record,
            expected_revision=expected_revision,
            grant=grant,
            audit=AuditEvent(
                id=f"audit_{uuid.uuid4().hex}",
                request_id=request_id,
                action=f"admin.registration.{action}",
                user_id=user_id,
                company_id=record.source_set.company_id,
                data_source_set_id=record.source_set.id,
                data_access_profile_id=record.source_set.data_access_profile_id,
                status="success",
                details=details,
            ),
        )
