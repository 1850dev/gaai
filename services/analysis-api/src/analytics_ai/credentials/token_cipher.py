"""Cloud KMS envelope boundary for persisted Google OAuth refresh tokens."""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Protocol

import google.auth
import httpx
from google.auth import credentials as google_credentials
from google.auth.transport.requests import Request

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
KMS_KEY_NAME = re.compile(
    r"^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$"
)


class TokenCipher(Protocol):
    async def encrypt(self, plaintext: str, *, user_id: str) -> str: ...

    async def decrypt(self, ciphertext: str, *, user_id: str) -> str: ...


class KmsTokenCipher:
    """Encrypt tokens before Firestore persistence; plaintext never leaves Backend."""

    def __init__(self, key_name: str, *, timeout_seconds: float = 15.0) -> None:
        if KMS_KEY_NAME.fullmatch(key_name) is None:
            raise ValueError("invalid Cloud KMS key resource name")
        self._key_name = key_name
        self._timeout_seconds = timeout_seconds

    async def encrypt(self, plaintext: str, *, user_id: str) -> str:
        if not plaintext:
            raise ValueError("refresh token is empty")
        payload = {
            "plaintext": self._encode(plaintext.encode()),
            "additionalAuthenticatedData": self._encode(self._aad(user_id)),
        }
        response = await self._request("encrypt", payload)
        ciphertext = response.get("ciphertext")
        if not isinstance(ciphertext, str) or not ciphertext:
            raise RuntimeError("Cloud KMS encrypt response omitted ciphertext")
        return ciphertext

    async def decrypt(self, ciphertext: str, *, user_id: str) -> str:
        if not ciphertext:
            raise ValueError("encrypted refresh token is empty")
        payload = {
            "ciphertext": ciphertext,
            "additionalAuthenticatedData": self._encode(self._aad(user_id)),
        }
        response = await self._request("decrypt", payload)
        plaintext = response.get("plaintext")
        if not isinstance(plaintext, str) or not plaintext:
            raise RuntimeError("Cloud KMS decrypt response omitted plaintext")
        try:
            return base64.b64decode(plaintext, validate=True).decode()
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("Cloud KMS returned invalid plaintext") from error

    async def _request(
        self,
        operation: str,
        payload: dict[str, str],
    ) -> dict[str, object]:
        credentials, _ = await asyncio.to_thread(
            google.auth.default,
            scopes=[CLOUD_PLATFORM_SCOPE],
        )
        token = await asyncio.to_thread(self._access_token, credentials)
        url = f"https://cloudkms.googleapis.com/v1/{self._key_name}:{operation}"
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("Cloud KMS returned an invalid response")
        return result

    @staticmethod
    def _access_token(credentials: google_credentials.Credentials) -> str:
        if not credentials.valid or credentials.expired:
            credentials.refresh(Request())  # type: ignore[no-untyped-call]
        if not credentials.token:
            raise RuntimeError("Cloud KMS credential did not return an access token")
        return str(credentials.token)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.b64encode(value).decode()

    @staticmethod
    def _aad(user_id: str) -> bytes:
        return f"analytics-ai:ga4-refresh-token:{user_id}".encode()
