"""Ephemeral LiveKit calls bound to the existing SillyTavern session contract.

The token service stays on the voice host: its LiveKit signing secret never
needs to be copied here. Workers authenticate with a separate per-call token.
"""
import asyncio
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import WS_TOKEN
from .profile_policy import ProfileNotAllowedError, profile_not_allowed_body, resolve_profile_selection
from .realtime import ws_broadcast
from .relay import forward_hermes_streaming
from .state import SESSION_INFOS

router = APIRouter(prefix="/v1/voice")

TOOL_STATUS_PHRASES = (
    "Let me take a look. ",
    "One moment. ",
    "I'll check that. ",
    "I'm running a few checks. ",
    "I'm on it. ",
    "This needs a few steps; I'll keep going. ",
    "I'm looking into it. ",
    "I'm continuing the check. ",
    "This takes a few steps; I'll continue. ",
    "I'm making progress; I'll update you when I find something. ",
)
TOOL_STATUS_MIN_INTERVAL = 5.0


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200000)


class StartCall(BaseModel):
    session_id: str = Field(min_length=1, max_length=512)
    messages: list[Message] = Field(default_factory=list, max_length=2000)
    model: str = Field(default="", max_length=200)
    profile: str = Field(default="", max_length=64)
    workspace: str = Field(default="", max_length=4096)


class Turn(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=2000)


class VoiceEvent(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    role: Literal["user", "assistant"]
    text: str = Field(max_length=200000)
    interrupted: bool = False


class VoiceError(BaseModel):
    message: str = Field(min_length=1, max_length=500)


@dataclass
class Call:
    id: str
    token: str
    request: StartCall
    touched: float = field(default_factory=time.monotonic)
    events: list[dict] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    rebuild: bool = False
    finished: bool = False
    end_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    error: str = ""
    tool_status_index: int = field(
        default_factory=lambda: secrets.randbelow(len(TOOL_STATUS_PHRASES))
    )
    last_tool_status_at: float = 0.0

    def next_tool_status(self, now: float) -> str | None:
        if self.last_tool_status_at and now - self.last_tool_status_at < TOOL_STATUS_MIN_INTERVAL:
            return None
        phrase = TOOL_STATUS_PHRASES[self.tool_status_index % len(TOOL_STATUS_PHRASES)]
        self.tool_status_index += 1
        self.last_tool_status_at = now
        return phrase


async def close_call(call: Call):
    from .app import hermes_ws_manager
    async with call.end_lock:
        if call.closed:
            return
        try:
            await hermes_ws_manager.interrupt_session(call.request.session_id)
        except ValueError:
            pass
        # Keep ownership until the mapping is gone. A repeated hangup must not
        # close a new text conversation that started after this call ended.
        await hermes_ws_manager.close_session(call.request.session_id)
        call.closed = True


calls: dict[str, Call] = {}


def browser_auth(request: Request):
    if WS_TOKEN and not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {WS_TOKEN}"):
        raise HTTPException(401, "Proxy authentication required")


def get_call(call_id: str) -> Call:
    call = calls.get(call_id)
    if not call or time.monotonic() - call.touched > 7200:
        raise HTTPException(410, "Call expired; start a new call")
    call.touched = time.monotonic()
    return call


def worker_call(call_id: str, request: Request) -> Call:
    call = get_call(call_id)
    if not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {call.token}"):
        raise HTTPException(401, "Invalid call token")
    return call


def active_call(session_id: str) -> bool:
    return any(not c.closed and c.request.session_id == session_id and time.monotonic() - c.touched < 7200 for c in calls.values())


@router.get("/capabilities")
async def capabilities(request: Request):
    browser_auth(request)
    return {"enabled": bool(os.getenv("VOICE_TOKEN_URL")), "protocol": 1}


@router.post("/calls")
async def start_call(body: StartCall, request: Request):
    browser_auth(request)
    try:
        resolve_profile_selection(body.profile)
    except ProfileNotAllowedError:
        return JSONResponse(profile_not_allowed_body(), status_code=403)
    token_url = os.getenv("VOICE_TOKEN_URL", "").strip()
    if not token_url:
        raise HTTPException(503, "VOICE_TOKEN_URL is not configured on proxy-ST")
    for key, call in list(calls.items()):
        if time.monotonic() - call.touched > 7200:
            calls.pop(key, None)
    if active_call(body.session_id):
        raise HTTPException(409, "An active call already owns this chat")
    if SESSION_INFOS.get(body.session_id, {}).get("agent_status", {}).get("active"):
        raise HTTPException(409, "Wait for the current Hermes response before calling")
    call = Call(uuid.uuid4().hex, secrets.token_urlsafe(32), body)
    calls[call.id] = call  # reserve before awaiting the token broker
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            result = await client.post(token_url, json={"room_config": {"agents": [{
                "agent_name": "local-voice",
                "metadata": json.dumps({"proxy_call_id": call.id, "proxy_call_token": call.token}),
            }]}})
            result.raise_for_status()
            connection = result.json()
            if not all(connection.get(key) for key in ("serverUrl", "participantToken", "roomName")):
                raise ValueError("Invalid token service response")
    except (httpx.HTTPError, ValueError) as exc:
        calls.pop(call.id, None)
        raise HTTPException(502, "LiveKit token service unavailable") from exc
    return {"callId": call.id, **connection}


@router.get("/calls/{call_id}/context")
async def call_context(call_id: str, request: Request):
    call = worker_call(call_id, request)
    if call.closed or call.end_lock.locked():
        raise HTTPException(410, "Call ended")
    return call.request.model_dump()


@router.get("/calls/{call_id}/events")
async def call_events(call_id: str, request: Request, after: int = 0):
    browser_auth(request)
    call = get_call(call_id)
    return {"events": call.events[max(0, after):], "closed": call.closed, "finished": call.finished, "error": call.error}


@router.post("/calls/{call_id}/error")
async def call_error(call_id: str, body: VoiceError, request: Request):
    call = worker_call(call_id, request)
    call.error = body.message
    return {"ok": True}


@router.post("/calls/{call_id}/finish")
async def finish_call(call_id: str, request: Request):
    call = worker_call(call_id, request)
    await close_call(call)
    call.finished = True
    return {"ok": True}


@router.post("/calls/{call_id}/events")
async def add_event(call_id: str, body: VoiceEvent, request: Request):
    call = worker_call(call_id, request)
    if body.id not in call.seen:
        call.seen.add(body.id)
        event = {"type": "voice_message", "call_id": call.id, "session_id": call.request.session_id,
                 "sequence": len(call.events) + 1, **body.model_dump()}
        call.events.append(event)
        call.rebuild = call.rebuild or body.interrupted
        await ws_broadcast(call.request.session_id, event)
    return {"ok": True}


@router.post("/calls/{call_id}/completions")
async def complete(call_id: str, body: Turn, request: Request):
    call = worker_call(call_id, request)
    if call.closed or call.end_lock.locked():
        raise HTTPException(410, "Call ended")
    if call.lock.locked():
        raise HTTPException(409, "Previous voice turn is still ending")
    await call.lock.acquire()
    from .app import hermes_ws_manager
    turn_id = uuid.uuid4().hex

    async def broadcast_text_delta(text: str):
        await ws_broadcast(
            call.request.session_id,
            {
                "type": "voice_response_delta",
                "call_id": call.id,
                "session_id": call.request.session_id,
                "turn_id": turn_id,
                "text": text,
            },
        )

    async def tool_status(_tool: dict) -> str | None:
        return call.next_tool_status(time.monotonic())

    try:
        if call.rebuild:
            await hermes_ws_manager.close_session(call.request.session_id)
            call.rebuild = False
        payload = {"model": "hermes-agent", "stream": True,
                   "messages": [m.model_dump() for m in body.messages],
                   "st_proxy": {"session_id": call.request.session_id, "backend": "hermes",
                                **({"model": call.request.model} if call.request.model else {}),
                                "profile": call.request.profile,
                                "workspace": call.request.workspace}}
        response = await forward_hermes_streaming(
            payload,
            call.request.session_id,
            hermes_ws_manager,
            text_delta_callback=broadcast_text_delta,
            tool_status_callback=tool_status,
        )
    except BaseException:
        call.lock.release()
        raise
    if not isinstance(response, StreamingResponse):
        call.lock.release()
        return response

    async def stream():
        try:
            async for chunk in response.body_iterator:
                yield chunk
        finally:
            await response.body_iterator.aclose()
            call.lock.release()
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.delete("/calls/{call_id}")
async def end_call(call_id: str, request: Request):
    browser_auth(request)
    call = get_call(call_id)
    await close_call(call)
    return {"ok": True}
