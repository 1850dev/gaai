"""Firebase ID-token verifiers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import firebase_admin
from firebase_admin import auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """The caller did not present a valid human identity."""


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    uid: str
    email: str | None
    google_subject: str | None = None


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedIdentity: ...


class FirebaseTokenVerifier:
    """Verify Firebase JWTs without exposing token material to logs."""

    _CLOCK_SKEW_SECONDS = 60

    def __init__(self, project_id: str, *, check_revoked: bool = True) -> None:
        self._project_id = project_id
        self._check_revoked = check_revoked
        self._app: firebase_admin.App | None = None
        if check_revoked:
            try:
                self._app = firebase_admin.get_app()
            except ValueError:
                self._app = firebase_admin.initialize_app(
                    options={"projectId": project_id}
                )

    async def verify(self, token: str) -> VerifiedIdentity:
        try:
            claims = await (
                self._verify_with_revocation(token)
                if self._check_revoked
                else self._verify_with_public_certificates(token)
            )
            return self._identity_from_claims(claims)
        except Exception as error:
            logger.warning(
                "Firebase ID token rejected",
                extra={
                    "error_code": "invalid_authentication",
                    "firebase_error_type": type(error).__name__,
                },
            )
            raise AuthenticationError("invalid Firebase ID token") from error

    async def _verify_with_revocation(self, token: str) -> Mapping[str, Any]:
        if self._app is None:
            raise RuntimeError("Firebase Admin app is not initialized")
        return await asyncio.to_thread(
            auth.verify_id_token,
            token,
            self._app,
            True,
        )

    async def _verify_with_public_certificates(
        self,
        token: str,
    ) -> Mapping[str, Any]:
        claims = await asyncio.to_thread(
            google_id_token.verify_firebase_token,
            token,
            GoogleAuthRequest(),
            audience=self._project_id,
            clock_skew_in_seconds=self._CLOCK_SKEW_SECONDS,
        )
        verified_claims = cast(Mapping[str, Any], claims)
        self._validate_firebase_claims(verified_claims)
        return verified_claims

    def _validate_firebase_claims(self, claims: Mapping[str, Any]) -> None:
        expected_issuer = f"https://securetoken.google.com/{self._project_id}"
        if claims.get("iss") != expected_issuer:
            raise ValueError("Firebase token issuer mismatch")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 128:
            raise ValueError("Firebase token subject is invalid")

        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, int):
            raise ValueError("Firebase token auth_time is invalid")
        if auth_time > int(time.time()) + self._CLOCK_SKEW_SECONDS:
            raise ValueError("Firebase token auth_time is in the future")

    @staticmethod
    def _identity_from_claims(claims: Mapping[str, Any]) -> VerifiedIdentity:
        uid = claims.get("uid") or claims.get("sub")
        if not isinstance(uid, str) or not uid:
            raise ValueError("Firebase token has no UID")
        email = claims.get("email")
        google_subject: str | None = None
        firebase_claims = claims.get("firebase")
        if isinstance(firebase_claims, Mapping):
            identities = firebase_claims.get("identities")
            if isinstance(identities, Mapping):
                google_identities = identities.get("google.com")
                if (
                    isinstance(google_identities, list)
                    and google_identities
                    and isinstance(google_identities[0], str)
                ):
                    google_subject = google_identities[0]
        return VerifiedIdentity(
            uid=uid,
            email=email if isinstance(email, str) else None,
            google_subject=google_subject,
        )


class DevelopmentTokenVerifier:
    """Explicitly unsafe verifier for isolated local development only."""

    async def verify(self, token: str) -> VerifiedIdentity:
        parts = token.split(":", maxsplit=2)
        if len(parts) not in {2, 3} or parts[0] != "dev" or not parts[1]:
            raise AuthenticationError("invalid development token")
        return VerifiedIdentity(
            uid=parts[1],
            email=parts[2] if len(parts) == 3 else None,
            google_subject=parts[1],
        )
