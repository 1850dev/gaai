"""Validated runtime configuration with production safety checks."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RepositoryBackend = Literal["auto", "memory", "firestore"]
ResolvedRepositoryBackend = Literal["memory", "firestore"]
CacheBackend = Literal["auto", "memory", "gcs", "filesystem"]
ResolvedCacheBackend = Literal["memory", "gcs", "filesystem"]


class Settings(BaseSettings):
    """Fail-closed configuration for human OAuth and workload credentials."""

    model_config = SettingsConfigDict(
        env_prefix="ANALYTICS_AI_",
        env_file=".env",
        extra="ignore",
    )

    environment: Literal["local", "test", "dev", "stg", "prod"] = "local"
    repository_backend: RepositoryBackend = "auto"
    firebase_project_id: str = "analytics-ai-dev"
    platform_project_id: str = "analytics-ai-dev"
    cache_bucket: str = "analytics-ai-dev-dataset-cache"
    cache_backend: CacheBackend = "auto"
    cache_directory: str = "/var/lib/analytics-ai/cache"
    enable_analytics_tools: bool = False
    enable_browser_qwen: bool = False
    enable_browser_agent: bool = False
    # When enabled, cloud inference is opt-in per Firebase UID, never per request.
    vertex_user_ids: frozenset[str] = Field(default_factory=frozenset)
    enable_bigquery_tools: bool = False
    enable_admin_connection_checks: bool = False
    enable_ga4_user_oauth: bool = False
    ga4_oauth_client_id: str = ""
    ga4_oauth_client_secret: SecretStr = SecretStr("")
    ga4_oauth_redirect_uri: str = "http://localhost:8080/api/integrations/ga4/callback"
    ga4_oauth_return_url: str = "http://localhost:5173"
    ga4_oauth_kms_key_name: str = ""
    ga4_oauth_transaction_ttl_seconds: int = Field(default=600, ge=300, le=900)
    allow_insecure_dev_auth: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    vertex_location: str = "global"
    vertex_model: str = "gemini-3.8-flash"
    bigquery_mcp_endpoint: str = "https://bigquery.googleapis.com/mcp"
    bigquery_max_dry_run_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        gt=0,
    )
    bigquery_max_result_rows: int = Field(default=3_000, ge=1, le=3_000)
    external_request_timeout_seconds: float = Field(default=180.0, ge=1.0, le=180.0)
    analysis_timeout_seconds: float = Field(default=200.0, ge=5.0, le=240.0)
    answer_composition_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
    )
    ga4_max_attempts: int = Field(default=3, ge=1, le=4)
    ga4_retry_base_seconds: float = Field(default=0.25, ge=0.0, le=5.0)

    @property
    def resolved_repository_backend(self) -> ResolvedRepositoryBackend:
        if self.repository_backend == "auto":
            return "memory" if self.environment in {"local", "test"} else "firestore"
        return self.repository_backend

    def vertex_allowed(self, uid: str) -> bool:
        return not self.enable_browser_qwen or uid in self.vertex_user_ids

    @property
    def resolved_cache_backend(self) -> ResolvedCacheBackend:
        if self.cache_backend == "auto":
            return "memory" if self.environment in {"local", "test"} else "gcs"
        return self.cache_backend

    @model_validator(mode="after")
    def enforce_environment_safety(self) -> Settings:
        if self.cache_backend == "filesystem" and (
            self.environment not in {"local", "test"}
            or self.resolved_repository_backend != "firestore"
        ):
            raise ValueError(
                "filesystem cache requires local environment and Firestore metadata"
            )
        if self.analysis_timeout_seconds <= self.external_request_timeout_seconds:
            raise ValueError("analysis timeout must exceed external request timeout")
        if self.answer_composition_timeout_seconds >= self.analysis_timeout_seconds:
            raise ValueError(
                "answer composition timeout must be shorter than analysis timeout"
            )
        if self.environment in {"dev", "stg", "prod"} and self.allow_insecure_dev_auth:
            raise ValueError("insecure development authentication is forbidden")
        if (
            self.environment in {"dev", "stg", "prod"}
            and self.repository_backend == "memory"
        ):
            raise ValueError("non-local environments must use Firestore")
        if (
            self.environment in {"dev", "stg", "prod"}
            and self.cache_backend == "memory"
        ):
            raise ValueError("non-local environments must use GCS dataset cache")
        if self.environment == "prod":
            if not self.firebase_project_id or not self.platform_project_id:
                raise ValueError("production project IDs must be configured")
            if self.platform_project_id != "analytics-ai-prod":
                raise ValueError(
                    "the production query project must be analytics-ai-prod"
                )
        if self.enable_ga4_user_oauth:
            required = {
                "ga4_oauth_client_id": self.ga4_oauth_client_id,
                "ga4_oauth_client_secret": (
                    self.ga4_oauth_client_secret.get_secret_value()
                ),
                "ga4_oauth_redirect_uri": self.ga4_oauth_redirect_uri,
                "ga4_oauth_kms_key_name": self.ga4_oauth_kms_key_name,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "GA4 USER_OAUTH configuration is incomplete: "
                    + ", ".join(sorted(missing))
                )
        return self
