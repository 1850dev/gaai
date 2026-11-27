"""Validated records accepted by the development bootstrap command."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field, model_validator

from analytics_ai.domain.models import (
    Company,
    CompanyMember,
    DataAccessProfile,
    DataSourceSet,
    FrozenModel,
    SemanticDefinition,
    User,
    UserAccessGrant,
)


class BootstrapManifest(FrozenModel):
    """One create-only, tenant-scoped batch of development records."""

    actor_user_id: str = Field(min_length=1, max_length=128)
    company_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    users: tuple[User, ...] = ()
    companies: tuple[Company, ...] = ()
    company_members: tuple[CompanyMember, ...] = ()
    data_access_profiles: tuple[DataAccessProfile, ...] = ()
    data_source_sets: tuple[DataSourceSet, ...] = ()
    user_access_grants: tuple[UserAccessGrant, ...] = ()
    semantic_definitions: tuple[SemanticDefinition, ...] = ()
    reason: str = Field(min_length=3, max_length=300)

    @model_validator(mode="after")
    def enforce_scope_and_disabled_resources(self) -> BootstrapManifest:
        record_groups = (
            self.users,
            self.companies,
            self.company_members,
            self.data_access_profiles,
            self.data_source_sets,
            self.user_access_grants,
            self.semantic_definitions,
        )
        if not any(record_groups):
            raise ValueError("bootstrap needs at least one record")

        self._require_unique((user.uid for user in self.users), "user")
        self._require_unique((company.id for company in self.companies), "company")
        self._require_unique(
            (
                f"{member.company_id}/{member.user_id}"
                for member in self.company_members
            ),
            "company member",
        )
        self._require_unique(
            (profile.id for profile in self.data_access_profiles), "profile"
        )
        self._require_unique(
            (source_set.id for source_set in self.data_source_sets),
            "Data Source Set",
        )
        self._require_unique((grant.id for grant in self.user_access_grants), "grant")
        self._require_unique(
            (definition.id for definition in self.semantic_definitions),
            "semantic definition",
        )

        for user in self.users:
            if user.uid != self.actor_user_id:
                raise ValueError("bootstrap can only create the actor user")
        for company in self.companies:
            if company.id != self.company_id:
                raise ValueError("company is outside the manifest scope")
        for member in self.company_members:
            if member.user_id != self.actor_user_id:
                raise ValueError("membership can only target the actor user")
            if member.company_id != self.company_id:
                raise ValueError("membership is outside the manifest company")
        for profile in self.data_access_profiles:
            if profile.company_id != self.company_id:
                raise ValueError("profile is outside the manifest company")
            if profile.ready:
                raise ValueError(
                    "bootstrap cannot mark a profile as verified and active"
                )
        for source_set in self.data_source_sets:
            if source_set.company_id != self.company_id:
                raise ValueError("Data Source Set is outside the manifest company")
            if source_set.enabled:
                raise ValueError("bootstrap cannot enable a Data Source Set")
        for grant in self.user_access_grants:
            if grant.user_id != self.actor_user_id:
                raise ValueError("grant can only target the actor user")
        for definition in self.semantic_definitions:
            if definition.company_id != self.company_id:
                raise ValueError(
                    "semantic definition must use the manifest company scope"
                )
        return self

    @staticmethod
    def _require_unique(values: Iterable[str], label: str) -> None:
        items = tuple(values)
        if len(items) != len(set(items)):
            raise ValueError(f"{label} IDs must be unique")
