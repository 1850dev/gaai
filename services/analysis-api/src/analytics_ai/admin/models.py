"""Registration contracts separate from published analytical scopes."""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from analytics_ai.domain.models import (
    DataSourceSet,
    FrozenModel,
    Ga4AuthMode,
    Ga4Source,
    Ga4StreamScope,
)


class RegisteredGa4Source(Ga4Source):
    # Never infer full-property approval or timezone/currency from omitted input.
    timezone: str = Field(min_length=1, max_length=100)
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
    stream_scopes: tuple[Ga4StreamScope, ...] = Field(min_length=1, max_length=20)
    auth_mode: Ga4AuthMode

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("unknown timezone") from error
        return value


class RegistrationInput(FrozenModel):
    name: str = Field(min_length=1, max_length=200)
    ga4: RegisteredGa4Source
    data_access_profile_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{2,127}$"
    )

    @model_validator(mode="after")
    def explicit_profile(self) -> "RegistrationInput":
        needs_profile = self.ga4.auth_mode is Ga4AuthMode.SERVICE_ACCOUNT
        if needs_profile != (self.data_access_profile_id is not None):
            raise ValueError(
                "only explicit SERVICE_ACCOUNT registrations require a profile"
            )
        return self


class RegistrationProof(FrozenModel):
    source_hash: str
    verified_by: str
    verified_at: datetime
    credential_identity: str
    profile_hash: str | None = None
    stream_metadata_parent: str | None = None


class VerificationResult(FrozenModel):
    credential_identity: str
    stream_metadata_parent: str


class SourceRegistration(FrozenModel):
    source_set: DataSourceSet
    revision: int = Field(ge=1)
    status: Literal["draft", "verified", "active", "disabled"] = "draft"
    proof: RegistrationProof | None = None
    legacy_source_hash: str | None = None
    legacy_grants_hash: str | None = None


class RevisionRequest(FrozenModel):
    expected_revision: int = Field(ge=1)


class UpdateRegistrationRequest(RevisionRequest):
    definition: RegistrationInput


class LegacyRegistrationRequest(FrozenModel):
    definition: RegistrationInput
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    grants_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DisableRegistrationRequest(FrozenModel):
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    grants_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int | None = Field(default=None, ge=1)


class GrantRequest(RevisionRequest):
    user_id: str = Field(min_length=1, max_length=128, pattern=r"^[^/]+$")
    enabled: bool


class RegistrationError(Exception):
    def __init__(self, code: str, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
