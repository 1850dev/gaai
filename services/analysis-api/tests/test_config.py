from __future__ import annotations

import pytest
from pydantic import ValidationError

from analytics_ai.config import Settings


def test_local_repository_defaults_to_memory() -> None:
    settings = Settings(environment="local", repository_backend="auto")
    assert settings.resolved_repository_backend == "memory"


def test_local_firestore_must_be_explicit() -> None:
    settings = Settings(environment="local", repository_backend="firestore")
    assert settings.resolved_repository_backend == "firestore"


def test_local_cache_defaults_to_memory_even_with_firestore_repository() -> None:
    settings = Settings(
        environment="local",
        repository_backend="firestore",
        cache_backend="auto",
    )
    assert settings.resolved_cache_backend == "memory"


def test_local_gcs_cache_must_be_explicit() -> None:
    settings = Settings(environment="local", cache_backend="gcs")
    assert settings.resolved_cache_backend == "gcs"


def test_admin_connection_gate_does_not_enable_analysis_tools() -> None:
    settings = Settings(
        environment="test",
        enable_admin_connection_checks=True,
    )

    assert settings.enable_admin_connection_checks is True
    assert settings.enable_analytics_tools is False


def test_analysis_timeout_must_exceed_external_timeout() -> None:
    with pytest.raises(ValidationError, match="analysis timeout must exceed"):
        Settings(
            environment="test",
            external_request_timeout_seconds=30,
            analysis_timeout_seconds=30,
        )


def test_answer_composition_timeout_defaults_below_analysis_timeout() -> None:
    settings = Settings(environment="test")

    assert settings.answer_composition_timeout_seconds == 30
    assert (
        settings.answer_composition_timeout_seconds < settings.analysis_timeout_seconds
    )


def test_answer_composition_timeout_must_be_shorter_than_analysis_timeout() -> None:
    with pytest.raises(
        ValidationError,
        match="answer composition timeout must be shorter than analysis timeout",
    ):
        Settings(
            environment="test",
            external_request_timeout_seconds=10,
            analysis_timeout_seconds=30,
            answer_composition_timeout_seconds=30,
        )


def test_ga4_user_oauth_is_fail_closed_without_complete_configuration() -> None:
    with pytest.raises(ValidationError, match="GA4 USER_OAUTH configuration"):
        Settings(environment="test", enable_ga4_user_oauth=True)


def test_ga4_user_oauth_gate_does_not_enable_analytics_tools() -> None:
    settings = Settings(
        environment="test",
        enable_ga4_user_oauth=True,
        ga4_oauth_client_id="test.apps.googleusercontent.com",
        ga4_oauth_client_secret="test-only",  # noqa: S106
        ga4_oauth_kms_key_name=(
            "projects/test/locations/global/keyRings/ga4/cryptoKeys/oauth"
        ),
    )

    assert settings.enable_ga4_user_oauth is True
    assert settings.enable_analytics_tools is False


@pytest.mark.parametrize("environment", ["dev", "stg", "prod"])
def test_non_local_environment_rejects_memory(environment: str) -> None:
    platform_project_id = (
        "analytics-ai-prod" if environment == "prod" else "analytics-ai-dev"
    )
    with pytest.raises(ValidationError, match="must use Firestore"):
        Settings(
            environment=environment,
            repository_backend="memory",
            platform_project_id=platform_project_id,
        )


@pytest.mark.parametrize("environment", ["dev", "stg", "prod"])
def test_non_local_environment_rejects_memory_cache(environment: str) -> None:
    platform_project_id = (
        "analytics-ai-prod" if environment == "prod" else "analytics-ai-dev"
    )
    with pytest.raises(ValidationError, match="must use GCS dataset cache"):
        Settings(
            environment=environment,
            cache_backend="memory",
            platform_project_id=platform_project_id,
        )
