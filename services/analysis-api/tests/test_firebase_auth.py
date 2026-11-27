from __future__ import annotations

import importlib
from typing import Any

import firebase_admin
import pytest
from firebase_admin import auth
from google.oauth2 import id_token as google_id_token

from analytics_ai.api.app import build_services
from analytics_ai.auth.firebase import (
    AuthenticationError,
    FirebaseTokenVerifier,
    VerifiedIdentity,
)
from analytics_ai.config import Settings
from analytics_ai.repositories.memory import InMemoryRepository


async def test_local_firebase_verifier_uses_public_certificates_without_adc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    def verify_firebase_token(
        token: str,
        _request: object,
        *,
        audience: str,
        clock_skew_in_seconds: int,
    ) -> dict[str, object]:
        received.update(
            token=token,
            audience=audience,
            clock_skew_in_seconds=clock_skew_in_seconds,
        )
        return {
            "sub": "firebase-user",
            "email": "user@example.com",
            "iss": "https://securetoken.google.com/firebase-project",
            "auth_time": 1,
            "firebase": {
                "identities": {"google.com": ["google-subject"]},
                "sign_in_provider": "google.com",
            },
        }

    monkeypatch.setattr(
        google_id_token,
        "verify_firebase_token",
        verify_firebase_token,
    )
    monkeypatch.setattr(
        firebase_admin,
        "initialize_app",
        lambda **_kwargs: pytest.fail("local verification must not initialize Admin"),
    )

    identity = await FirebaseTokenVerifier(
        "firebase-project",
        check_revoked=False,
    ).verify("signed-token")

    assert identity == VerifiedIdentity(
        uid="firebase-user",
        email="user@example.com",
        google_subject="google-subject",
    )
    assert received == {
        "token": "signed-token",
        "audience": "firebase-project",
        "clock_skew_in_seconds": 60,
    }


async def test_revocation_verifier_uses_firebase_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firebase_app = object()
    received: dict[str, Any] = {}
    monkeypatch.setattr(firebase_admin, "get_app", lambda: firebase_app)

    def verify_id_token(
        token: str,
        app: object,
        should_check_revoked: bool,
    ) -> dict[str, str]:
        received.update(
            token=token,
            app=app,
            check_revoked=should_check_revoked,
        )
        return {"uid": "firebase-user", "email": "user@example.com"}

    monkeypatch.setattr(auth, "verify_id_token", verify_id_token)

    identity = await FirebaseTokenVerifier(
        "firebase-project",
        check_revoked=True,
    ).verify("signed-token")

    assert identity.uid == "firebase-user"
    assert received == {
        "token": "signed-token",
        "app": firebase_app,
        "check_revoked": True,
    }


@pytest.mark.parametrize(
    "claims",
    [
        {
            "sub": "user",
            "iss": "https://securetoken.google.com/wrong-project",
            "auth_time": 1,
        },
        {
            "sub": "",
            "iss": "https://securetoken.google.com/firebase-project",
            "auth_time": 1,
        },
        {
            "sub": "user",
            "iss": "https://securetoken.google.com/firebase-project",
            "auth_time": "invalid",
        },
    ],
)
async def test_local_firebase_verifier_rejects_invalid_firebase_claims(
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, object],
) -> None:
    monkeypatch.setattr(
        google_id_token,
        "verify_firebase_token",
        lambda *_args, **_kwargs: claims,
    )

    with pytest.raises(AuthenticationError):
        await FirebaseTokenVerifier(
            "firebase-project",
            check_revoked=False,
        ).verify("signed-token")


@pytest.mark.parametrize(
    ("environment", "expected_check_revoked"),
    [
        ("local", False),
        ("dev", True),
        ("stg", True),
        ("prod", True),
    ],
)
def test_service_factory_disables_revocation_check_only_for_local(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    expected_check_revoked: bool,
) -> None:
    app_module = importlib.import_module("analytics_ai.api.app")
    received: dict[str, Any] = {}

    class CapturingVerifier:
        async def verify(self, _token: str) -> VerifiedIdentity:
            return VerifiedIdentity(uid="unused", email=None)

    def verifier_factory(
        project_id: str,
        *,
        check_revoked: bool,
    ) -> CapturingVerifier:
        received.update(
            project_id=project_id,
            check_revoked=check_revoked,
        )
        return CapturingVerifier()

    monkeypatch.setattr(app_module, "FirebaseTokenVerifier", verifier_factory)
    platform_project_id = (
        "analytics-ai-prod" if environment == "prod" else "analytics-ai-dev"
    )

    build_services(
        Settings(
            environment=environment,
            firebase_project_id="firebase-project",
            platform_project_id=platform_project_id,
        ),
        repository=InMemoryRepository(),
    )

    assert received == {
        "project_id": "firebase-project",
        "check_revoked": expected_check_revoked,
    }
