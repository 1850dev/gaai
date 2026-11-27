"""Shared transaction-time checks for memory and Firestore registration writes."""

import hashlib
import json
from datetime import timedelta

from analytics_ai.admin.models import RegistrationError, SourceRegistration
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.domain.models import (
    Company,
    CompanyMember,
    DataAccessProfile,
    DataSourceSet,
    FrozenModel,
    Ga4AuthMode,
    MemberRole,
    Status,
    User,
    UserAccessGrant,
    utc_now,
)

ADMIN_ROLES = {MemberRole.COMPANY_ADMIN, MemberRole.PLATFORM_ADMIN}
PROOF_LIFETIME = timedelta(minutes=15)


def fingerprint(model: FrozenModel) -> str:
    payload = model.model_dump(mode="json")
    if isinstance(model, DataAccessProfile):
        payload["allowed_mcp_tools"] = sorted(model.allowed_mcp_tools)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def grants_fingerprint(grants: list[UserAccessGrant]) -> str:
    return hashlib.sha256(
        json.dumps(
            [g.model_dump(mode="json") for g in sorted(grants, key=lambda g: g.id)],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def check_legacy_scope(current: DataSourceSet, desired: DataSourceSet) -> None:
    if (
        current.ga4 is None
        or desired.ga4 is None
        or current.bigquery is not None
        or desired.bigquery is not None
        or (
            current.id,
            current.company_id,
            current.semantic_version,
            current.data_access_profile_id,
            current.ga4.property_id,
            current.ga4.auth_mode,
        )
        != (
            desired.id,
            desired.company_id,
            desired.semantic_version,
            desired.data_access_profile_id,
            desired.ga4.property_id,
            desired.ga4.auth_mode,
        )
        or not desired.ga4.stream_scopes
        or (
            current.ga4.stream_scopes
            and not set(desired.ga4.stream_scopes).issubset(current.ga4.stream_scopes)
        )
    ):
        raise RegistrationError("registration_legacy_scope_change")


def require_admin(
    user: User | None,
    member: CompanyMember | None,
    company: Company | None,
    company_id: str,
) -> None:
    if (
        user is None
        or user.status is not Status.ACTIVE
        or company is None
        or company.id != company_id
        or company.status is not Status.ACTIVE
        or member is None
        or member.status is not Status.ACTIVE
        or member.company_id != company_id
        or member.user_id != user.uid
        or member.role not in ADMIN_ROLES
    ):
        raise AccessDenied("registration_admin_required")


def require_profile(source: DataSourceSet, profile: DataAccessProfile | None) -> None:
    if source.ga4 is None:
        raise RegistrationError("registration_ga4_required", 422)
    if source.ga4.auth_mode is Ga4AuthMode.SERVICE_ACCOUNT:
        if (
            profile is None
            or not profile.ready
            or profile.company_id != source.company_id
        ):
            raise AccessDenied("registration_profile_unavailable")


def check_write(
    *,
    actor: User | None,
    member: CompanyMember | None,
    company: Company | None,
    profile: DataAccessProfile | None,
    previous: SourceRegistration | None,
    current_source: DataSourceSet | None,
    updated: SourceRegistration,
    expected_revision: int | None,
    grant: UserAccessGrant | None,
    recipient: User | None,
    recipient_member: CompanyMember | None,
    source_grants: list[UserAccessGrant] | None = None,
) -> None:
    source = updated.source_set
    require_admin(actor, member, company, source.company_id)
    if updated.status != "disabled" and (grant is None or grant.enabled):
        require_profile(source, profile)
    legacy = updated.legacy_source_hash is not None
    if (
        previous is not None
        and (
            previous.legacy_source_hash != updated.legacy_source_hash
            or previous.legacy_grants_hash != updated.legacy_grants_hash
        )
        and updated.status != "disabled"
    ):
        raise RegistrationError("registration_legacy_baseline_changed")
    if updated.status == "disabled":
        if (
            current_source is None
            or source != current_source.model_copy(update={"enabled": False})
            or fingerprint(current_source) != updated.legacy_source_hash
            or grants_fingerprint(source_grants or []) != updated.legacy_grants_hash
            or grant is not None
            or (previous.revision if previous else None) != expected_revision
            or updated.revision != (expected_revision or 0) + 1
            or updated.proof is not None
        ):
            raise RegistrationError("registration_disable_conflict")
        return
    if legacy and (previous is None or previous.status != "active"):
        if (
            current_source is None
            or fingerprint(current_source) != updated.legacy_source_hash
            or grants_fingerprint(source_grants or []) != updated.legacy_grants_hash
        ):
            raise RegistrationError("registration_legacy_review_stale")
        check_legacy_scope(current_source, source)
    if expected_revision is None:
        if previous is not None or (current_source is not None and not legacy):
            raise RegistrationError("registration_already_exists")
        if updated.revision != 1 or updated.status != "draft":
            raise RegistrationError("registration_invalid_transition")
    else:
        if previous is None or previous.revision != expected_revision:
            raise RegistrationError("registration_revision_conflict")
        if previous.source_set.company_id != source.company_id:
            raise AccessDenied("registration_company_change")
        if updated.revision != expected_revision + 1:
            raise RegistrationError("registration_revision_conflict")
        if previous.status == "active":
            if (
                grant is None
                or updated.status != "active"
                or source != previous.source_set
                or current_source != source
            ):
                raise RegistrationError("registration_active_immutable")
        elif previous.status == "disabled":
            raise RegistrationError("registration_disabled")
        elif current_source is not None and not legacy:
            raise RegistrationError("registration_published_source_conflict")
        if updated.status == "active" and previous.status != "active":
            if (
                previous.status != "verified"
                or updated.proof != previous.proof
                or source != previous.source_set.model_copy(update={"enabled": True})
            ):
                raise RegistrationError("registration_verification_required")
        if updated.status == "verified" and (
            previous.status != "draft" or source != previous.source_set
        ):
            raise RegistrationError("registration_invalid_transition")
    if updated.status == "draft" and updated.proof is not None:
        raise RegistrationError("registration_invalid_transition")
    if updated.status != "active" and source.enabled:
        raise RegistrationError("registration_invalid_transition")
    if updated.status in {"verified", "active"} and (
        previous is None or previous.status != "active"
    ):
        proof = updated.proof
        now = utc_now()
        canonical_source = source.model_copy(update={"enabled": False})
        if (
            proof is None
            or actor is None
            or proof.verified_by != actor.uid
            or proof.source_hash != fingerprint(canonical_source)
            or not timedelta(0) <= now - proof.verified_at <= PROOF_LIFETIME
            or proof.profile_hash != (fingerprint(profile) if profile else None)
        ):
            raise RegistrationError("registration_verification_stale")
    if grant is not None:
        if (
            updated.status != "active"
            or previous is None
            or previous.status != "active"
            or grant.data_source_set_id != source.id
            or grant.role is not MemberRole.ANALYST
        ):
            raise RegistrationError("registration_active_required")
        if grant.enabled and (
            recipient is None
            or recipient.uid != grant.user_id
            or recipient.status is not Status.ACTIVE
            or recipient_member is None
            or recipient_member.status is not Status.ACTIVE
            or recipient_member.company_id != source.company_id
            or recipient_member.user_id != grant.user_id
        ):
            raise AccessDenied("grant_recipient_unavailable")
