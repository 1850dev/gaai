"""One authorized turn over one socket; no cross-instance or token-in-URL state."""

import asyncio
from contextlib import aclosing
from typing import Any, Literal
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
)

from analytics_ai.agent.browser_director import BrowserAgentDirector
from analytics_ai.agent.service import AgentService
from analytics_ai.auth.firebase import AuthenticationError, TokenVerifier
from analytics_ai.authorization.service import AccessDenied
from analytics_ai.orchestrator.service import AnalysisOrchestrationError


class BrowserStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["start"]
    token: SecretStr = Field(min_length=1, max_length=8192)
    content: str = Field(min_length=1, max_length=8000)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    approval_protocol: Literal[1] | None = None


class InferenceReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["inference_result"]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    output: str = Field(max_length=30_000)
    ok: bool


class ApprovalReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["plan_confirmation"]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    approved: bool


reply_adapter: TypeAdapter[InferenceReply | ApprovalReply] = TypeAdapter(
    InferenceReply | ApprovalReply
)


async def read_text(socket: WebSocket) -> str:
    frame = await socket.receive()
    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect()
    value = frame.get("text")
    if not isinstance(value, str) or len(value) > 48_000:
        raise ValueError("browser_agent_invalid_frame")
    return value


async def serve_browser_agent(
    socket: WebSocket,
    session_id: str,
    verifier: TokenVerifier,
    agent: AgentService,
) -> None:
    await socket.accept()
    try:
        async with asyncio.timeout(15):
            start = BrowserStart.model_validate_json(await read_text(socket))
            identity = await verifier.verify(start.token.get_secret_value())
    except WebSocketDisconnect:
        return
    except (AuthenticationError, ValidationError, ValueError, TimeoutError):
        await socket.send_json(
            {"event": "error", "data": {"code": "invalid_authentication"}}
        )
        await socket.close(code=1008)
        return

    pending: asyncio.Future[InferenceReply] | None = None
    pending_approval: asyncio.Future[ApprovalReply] | None = None
    expected = ""

    async def inference(
        phase: str,
        instructions: str,
        context: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> str:
        nonlocal pending, expected
        expected = uuid4().hex
        pending = asyncio.get_running_loop().create_future()
        try:
            await socket.send_json(
                {
                    "event": "inference",
                    "data": {
                        "id": expected,
                        "phase": phase,
                        "instructions": instructions,
                        "context": context,
                        "schema": schema,
                        "max_tokens": max_tokens,
                    },
                }
            )
            reply = await pending
            if not reply.ok:
                raise AnalysisOrchestrationError("browser_inference_failed")
            return reply.output
        finally:
            pending, expected = None, ""

    async def receive() -> None:
        while True:
            reply = reply_adapter.validate_json(await read_text(socket))
            if reply.request_id != expected:
                raise ValueError("browser_agent_unexpected_reply")
            if isinstance(reply, ApprovalReply):
                if pending_approval is None or pending_approval.done():
                    raise ValueError("browser_agent_unexpected_confirmation")
                pending_approval.set_result(reply)
            else:
                if pending is None or pending.done():
                    raise ValueError("browser_agent_unexpected_inference")
                pending.set_result(reply)

    async def approval(plan: dict[str, Any]) -> bool:
        nonlocal pending_approval, expected
        if start.approval_protocol != 1:
            raise AnalysisOrchestrationError("browser_client_update_required")
        expected = uuid4().hex
        pending_approval = asyncio.get_running_loop().create_future()
        try:
            await socket.send_json(
                {
                    "event": "plan_confirmation",
                    "data": {"id": expected, **plan},
                }
            )
            async with asyncio.timeout(60):
                return (await pending_approval).approved
        except TimeoutError as error:
            raise AnalysisOrchestrationError(
                "browser_plan_confirmation_timeout"
            ) from error
        finally:
            pending_approval, expected = None, ""

    async def run() -> None:
        try:
            async with aclosing(
                agent.turn(
                    user_id=identity.uid,
                    session_id=session_id,
                    content=start.content,
                    operation_id=start.operation_id,
                    browser_director=BrowserAgentDirector(inference),
                    browser_approval=approval,
                )
            ) as events:
                async for event in events:
                    await socket.send_json(event)
        except AccessDenied:
            await socket.send_json(
                {"event": "error", "data": {"code": "access_denied"}}
            )
        except AnalysisOrchestrationError as error:
            await socket.send_json({"event": "error", "data": {"code": error.code}})

    reader = asyncio.create_task(receive())
    runner = asyncio.create_task(run())
    try:
        completed, _ = await asyncio.wait(
            (reader, runner), return_when=asyncio.FIRST_COMPLETED
        )
        if reader in completed:
            reader.result()
        else:
            runner.result()
    except WebSocketDisconnect:
        return
    except (ValidationError, ValueError):
        await socket.send_json(
            {"event": "error", "data": {"code": "browser_agent_invalid_frame"}}
        )
    finally:
        reader.cancel()
        runner.cancel()
        await asyncio.gather(reader, runner, return_exceptions=True)
    await socket.close()
