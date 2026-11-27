"""Small authenticated JSON-RPC client for Google BigQuery Remote MCP."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from google.auth import credentials as google_credentials

from analytics_ai.credentials.broker import CredentialBroker


class McpTransportError(RuntimeError):
    pass


class BigQueryRemoteMcpClient:
    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        credentials: google_credentials.Credentials,
    ) -> dict[str, Any]:
        access_token = await CredentialBroker.access_token(credentials)
        response = await self._client.post(
            self._endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": f"mcp_{uuid.uuid4().hex}",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": dict(arguments)},
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise McpTransportError("BigQuery MCP HTTP failure") from error
        payload = self._decode_response(response)
        if "error" in payload:
            raise McpTransportError("BigQuery MCP returned a JSON-RPC error")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpTransportError("BigQuery MCP response has no result")
        if isinstance(result.get("structuredContent"), dict):
            return dict(result["structuredContent"])
        for block in result.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
                if isinstance(decoded, dict):
                    return decoded
        return result

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            payload = response.json()
            if not isinstance(payload, dict):
                raise McpTransportError("invalid JSON-RPC response")
            return payload
        events: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line.removeprefix("data:").strip()
            if not raw or raw == "[DONE]":
                continue
            value = json.loads(raw)
            if isinstance(value, dict):
                events.append(value)
        if not events:
            raise McpTransportError("empty MCP event stream")
        return events[-1]
