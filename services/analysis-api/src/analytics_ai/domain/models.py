"""Typed domain contracts shared by authorization and analysis components."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from analytics_ai.compat import StrEnum


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Status(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ConnectionStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class Ga4AuthMode(StrEnum):
    USER_OAUTH = "USER_OAUTH"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"


class Ga4ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class Ga4PropertyAccessStatus(StrEnum):
    ACCESSIBLE = "accessible"
    DENIED = "denied"


class MemberRole(StrEnum):
    ANALYST = "analyst"
    COMPANY_ADMIN = "company_admin"
    PLATFORM_ADMIN = "platform_admin"


class User(FrozenModel):
    uid: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    status: Status = Status.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)


class Company(FrozenModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    name: str = Field(min_length=1, max_length=200)
    status: Status = Status.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)


class CompanyMember(FrozenModel):
    user_id: str
    company_id: str
    role: MemberRole = MemberRole.ANALYST
    status: Status = Status.ACTIVE


class DataAccessProfile(FrozenModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    company_id: str
    service_account_email: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{2,62}@[a-z0-9-]+\.iam\.gserviceaccount\.com$"
    )
    allowed_mcp_tools: frozenset[str] = Field(default_factory=frozenset)
    status: Status = Status.DISABLED
    connection_status: ConnectionStatus = ConnectionStatus.PENDING

    @property
    def ready(self) -> bool:
        return (
            self.status is Status.ACTIVE
            and self.connection_status is ConnectionStatus.VERIFIED
        )


class Ga4Platform(StrEnum):
    WEB = "web"
    IOS = "iOS"
    ANDROID = "Android"


class Ga4StreamScope(FrozenModel):
    stream_id: str = Field(pattern=r"^[0-9]+$")
    platform: Ga4Platform


class Ga4Source(FrozenModel):
    property_id: str = Field(pattern=r"^[0-9]+$")
    display_name: str = Field(min_length=1, max_length=200)
    auth_mode: Ga4AuthMode = Ga4AuthMode.USER_OAUTH
    timezone: str = "Asia/Tokyo"
    currency_code: str = Field(default="JPY", pattern=r"^[A-Z]{3}$")
    stream_scopes: tuple[Ga4StreamScope, ...] = ()

    @field_validator("stream_scopes")
    @classmethod
    def unique_stream_ids(
        cls, value: tuple[Ga4StreamScope, ...]
    ) -> tuple[Ga4StreamScope, ...]:
        stream_ids = [stream.stream_id for stream in value]
        if len(set(stream_ids)) != len(stream_ids):
            raise ValueError("GA4 stream IDs must be unique")
        return value


class BigQuerySource(FrozenModel):
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,62}$")
    dataset_ids: tuple[str, ...] = Field(min_length=1)
    allowed_resources: tuple[str, ...] = Field(min_length=1)
    bigquery_ai_enabled: bool = False
    allowed_ai_functions: frozenset[str] = Field(default_factory=frozenset)
    allowed_ai_columns: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("dataset_ids")
    @classmethod
    def unique_datasets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("dataset IDs must be unique")
        return value

    @field_validator("allowed_resources")
    @classmethod
    def validate_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for resource in value:
            parts = resource.split(".")
            if len(parts) != 3 or any(not part for part in parts):
                raise ValueError("resources must be project.dataset.table patterns")
        return value


class DataSourceSet(FrozenModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    company_id: str
    name: str = Field(min_length=1, max_length=200)
    data_access_profile_id: str | None = None
    ga4: Ga4Source | None = None
    bigquery: BigQuerySource | None = None
    semantic_version: str = Field(default="global-v1", min_length=1, max_length=100)
    enabled: bool = False

    @model_validator(mode="after")
    def require_source(self) -> DataSourceSet:
        if self.ga4 is None and self.bigquery is None:
            raise ValueError("a Data Source Set needs GA4 or BigQuery")
        if self.bigquery is not None:
            if self.data_access_profile_id is None:
                raise ValueError("BigQuery requires a Data Access Profile")
            missing = set(self.bigquery.dataset_ids) - {
                resource.split(".")[1] for resource in self.bigquery.allowed_resources
            }
            if missing:
                raise ValueError("every dataset must have an allowed resource")
        if (
            self.ga4 is not None
            and self.ga4.auth_mode is Ga4AuthMode.SERVICE_ACCOUNT
            and self.data_access_profile_id is None
        ):
            raise ValueError("GA4 SERVICE_ACCOUNT mode requires a Data Access Profile")
        return self


class UserAccessGrant(FrozenModel):
    id: str
    user_id: str
    data_source_set_id: str
    role: MemberRole = MemberRole.ANALYST
    enabled: bool = True


class DatePeriod(FrozenModel):
    start: date
    end: date

    @model_validator(mode="after")
    def dates_in_order(self) -> DatePeriod:
        if self.end < self.start:
            raise ValueError("period end must not precede start")
        return self


class AnalysisFocus(FrozenModel):
    metric: str | None = None
    dimension: str | None = None
    channel: str | None = None
    device: str | None = None


class AnalysisFilter(FrozenModel):
    """A trusted Semantic filter persisted independently from chat prose."""

    semantic_name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=60)
    field_name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=500)
    operator: str = Field(default="exact", pattern=r"^exact$")


class FindingRef(FrozenModel):
    id: str
    text: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    assumptions: tuple[str, ...] = ()


class AnalysisState(FrozenModel):
    data_source_set_id: str
    period: DatePeriod | None = None
    comparison_period: DatePeriod | None = None
    focus: AnalysisFocus = Field(default_factory=AnalysisFocus)
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    filters: tuple[AnalysisFilter, ...] = ()
    time_grain: str | None = None
    selected_skill: str | None = None
    skill_version: str | None = None
    dataset_ids: tuple[str, ...] = ()
    findings: tuple[FindingRef, ...] = ()
    semantic_version: str
    router_history: tuple[str, ...] = ()


class SessionStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AnalysisSession(FrozenModel):
    id: str
    title: str = Field(default="新しい分析", min_length=1, max_length=120)
    user_id: str
    company_id: str
    data_source_set_id: str
    data_access_profile_id: str | None = None
    source_scope_fingerprint: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    state: AnalysisState
    status: SessionStatus = SessionStatus.ACTIVE
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditEvent(FrozenModel):
    id: str
    request_id: str
    action: str
    user_id: str
    company_id: str | None = None
    data_source_set_id: str | None = None
    session_id: str | None = None
    data_access_profile_id: str | None = None
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Ga4UserConnection(FrozenModel):
    user_id: str = Field(min_length=1, max_length=128)
    google_subject: str = Field(min_length=1, max_length=255)
    google_email: str = Field(min_length=3, max_length=320)
    granted_scopes: frozenset[str] = Field(min_length=1)
    encrypted_refresh_token: str | None = Field(default=None, min_length=1)
    status: Ga4ConnectionStatus = Ga4ConnectionStatus.CONNECTED
    connected_at: datetime = Field(default_factory=utc_now)
    last_verified_at: datetime = Field(default_factory=utc_now)
    token_created_at: datetime = Field(default_factory=utc_now)
    token_refresh_at: datetime | None = None

    @model_validator(mode="after")
    def connected_connection_has_token(self) -> Ga4UserConnection:
        if (
            self.status is Ga4ConnectionStatus.CONNECTED
            and self.encrypted_refresh_token is None
        ):
            raise ValueError("connected GA4 connection requires a refresh token")
        return self


class Ga4OAuthTransaction(FrozenModel):
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_id: str = Field(min_length=1, max_length=128)
    expected_email: str = Field(min_length=3, max_length=320)
    expected_google_subject: str = Field(min_length=1, max_length=255)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=8, max_length=2048)
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)


class Ga4PropertyAccessRecord(FrozenModel):
    user_id: str = Field(min_length=1, max_length=128)
    property_id: str = Field(pattern=r"^[0-9]+$")
    status: Ga4PropertyAccessStatus
    last_checked_at: datetime = Field(default_factory=utc_now)


class SemanticScope(StrEnum):
    GLOBAL = "global"
    COMPANY = "company"
    DATA_SOURCE_SET = "data_source_set"


class SemanticDefinition(FrozenModel):
    id: str
    canonical_name: str
    aliases: tuple[str, ...] = Field(min_length=1)
    kind: str
    unit: str | None = None
    aggregation: str | None = None
    scope: SemanticScope
    company_id: str | None = None
    data_source_set_id: str | None = None
    ga4_mapping: dict[str, Any] | None = None
    bigquery_mapping: dict[str, Any] | None = None
    visualization_metadata: dict[str, Any] = Field(default_factory=dict)
    version: str
    enabled: bool = True
    available: bool = True

    @model_validator(mode="after")
    def validate_scope(self) -> SemanticDefinition:
        if self.scope is SemanticScope.GLOBAL:
            if self.company_id or self.data_source_set_id:
                raise ValueError("global definitions cannot carry tenant scope")
        elif self.scope is SemanticScope.COMPANY:
            if not self.company_id or self.data_source_set_id:
                raise ValueError("company definition requires only company_id")
        elif not self.company_id or not self.data_source_set_id:
            raise ValueError("Data Source Set definition requires both scope IDs")
        if not self.available and (self.ga4_mapping or self.bigquery_mapping):
            raise ValueError("unavailable definitions cannot carry data mappings")
        return self


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"


class ComparisonKind(StrEnum):
    MOM = "mom"
    YOY = "yoy"
    CUSTOM = "custom"


class CalculationKind(StrEnum):
    VALUE = "value"
    DIFFERENCE = "difference"
    RATIO = "ratio"
    CHANGE_RATE = "change_rate"
    RANK = "rank"
    CONTRIBUTION = "contribution"
    PERIOD_TOTAL = "period_total"
    PERIOD_AVERAGE = "period_average"
    LATEST_VALUE = "latest_value"
    ANOMALY_SCORE = "anomaly_score"


class CalculationFact(FrozenModel):
    """A deterministic numeric fact derived from one evidence-backed dataset."""

    kind: CalculationKind
    value: str
    category: str | None = None
    rank: int | None = Field(default=None, ge=1)
    numerator: str | None = None
    denominator: str | None = None
    comparison_kind: ComparisonKind | None = None
    unit: str | None = None


class AnalysisResultReference(FrozenModel):
    """Server-selected source of a chart attached to a later answer."""

    session_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    source_fingerprint: str = ""
    semantic_version: str = ""


class AnalysisMessage(FrozenModel):
    id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str
    user_id: str
    company_id: str
    data_source_set_id: str
    role: MessageRole
    kind: Literal["chat", "agent_execution"] = "chat"
    status: MessageStatus = MessageStatus.COMPLETED
    content: str = Field(min_length=1, max_length=16_000)
    content_format: Literal["plain", "markdown"] = "plain"
    dataset_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    visualization: dict[str, Any] | None = None
    result_references: tuple[AnalysisResultReference, ...] = Field(
        default=(), max_length=11
    )
    error_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CategoryComparison(FrozenModel):
    """Same-dimensional-tuple comparison; missing values are never measured zero."""

    dimensions: dict[str, str]
    current_value: str | None = None
    comparison_value: str | None = None
    difference: str | None = None
    change_rate: str | None = None
    status: Literal["matched", "current_only", "comparison_only", "ambiguous"]


class CategoryValue(FrozenModel):
    """One observed dimensional tuple, never a sum across overlapping groups."""

    dimensions: dict[str, str]
    value: str


class AnalysisFinding(FrozenModel):
    id: str = Field(pattern=r"^finding_[a-f0-9]{32}$")
    session_id: str
    user_id: str
    company_id: str
    data_source_set_id: str
    dataset_id: str
    query_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    semantic_version: str
    skill_id: str | None = None
    skill_version: str | None = None
    compiler_version: str | None = None
    text: str = Field(min_length=1, max_length=2_000)
    metric_field: str
    metric_label: str
    dimension_field: str | None = None
    dimension_kind: str | None = None
    dimension_fields: tuple[str, ...] = ()
    category_values: tuple[CategoryValue, ...] = Field(default=(), max_length=12)
    value: str | None = None
    comparison_value: str | None = None
    change_rate: str | None = None
    comparison_kind: ComparisonKind | None = None
    calculations: tuple[CalculationFact, ...] = ()
    category_comparisons: tuple[CategoryComparison, ...] = Field(
        default=(), max_length=20
    )
    assumptions: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    def to_ref(self) -> FindingRef:
        return FindingRef(
            id=self.id,
            text=self.text,
            evidence_ids=self.evidence_ids,
            assumptions=self.assumptions,
        )


class ReportQuality(FrozenModel):
    sampled: bool = False
    subject_to_thresholding: bool = False
    data_loss_from_other_row: bool = False
    empty_reason: str | None = None
    time_zone: str | None = None
    currency_code: str | None = None
    notes: tuple[str, ...] = ()


class DatasetEvidence(FrozenModel):
    id: str
    company_id: str
    data_source_set_id: str
    dataset_id: str
    source: str
    semantic_version: str
    skill_id: str | None = None
    skill_version: str | None = None
    compiler_version: str | None = None
    mcp_server: str | None = None
    tool_name: str | None = None
    ga4_property_id: str | None = None
    bigquery_project_id: str | None = None
    query_hash: str
    query_id: str | None = None
    bytes_processed: int | None = Field(default=None, ge=0)
    rows: int = Field(ge=0, le=3_000)
    cache_hit: bool = False
    latency_ms: int | None = Field(default=None, ge=0)
    quality: ReportQuality = Field(default_factory=ReportQuality)
    property_quota: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
