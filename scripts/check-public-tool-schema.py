#!/usr/bin/env python3
"""Verify no tool schemas are exposed by a disposable, freshly built Hermes session."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from urllib.parse import urlencode

import httpx
import websockets

RPC_TIMEOUT = 30
AGENT_BUILD_TIMEOUT = 600


async def receive(socket, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Hermes gateway response timed out")
    frame = await asyncio.wait_for(socket.recv(), timeout=remaining)
    message = json.loads(frame)
    if not isinstance(message, dict):
        raise RuntimeError("Hermes gateway returned a non-object frame")
    return message


async def rpc(socket, method: str, params: dict, events: deque[dict]) -> dict:
    request_id = f"phase2-{method}-{time.monotonic_ns()}"
    await socket.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
    )
    deadline = time.monotonic() + RPC_TIMEOUT
    while True:
        message = await receive(socket, deadline)
        if message.get("method") == "event":
            events.append(message)
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(f"Hermes {method} returned a JSON-RPC error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Hermes {method} returned no result object")
        return result


async def wait_for_agent(socket, session_id: str, events: deque[dict]) -> None:
    """Wait for the gateway's documented post-build session.info event; never submit a prompt."""
    deadline = time.monotonic() + AGENT_BUILD_TIMEOUT
    while True:
        for event in list(events):
            params = event.get("params") or {}
            if params.get("session_id") != session_id:
                continue
            if params.get("type") == "session.info":
                events.remove(event)
                return
            if params.get("type") == "error":
                raise RuntimeError("Hermes agent construction failed")

        message = await receive(socket, deadline)
        if message.get("method") != "event":
            continue
        params = message.get("params") or {}
        if params.get("session_id") != session_id:
            events.append(message)
        elif params.get("type") == "session.info":
            return
        elif params.get("type") == "error":
            raise RuntimeError("Hermes agent construction failed")


async def main() -> int:
    dashboard_url = os.environ["HERMES_DASHBOARD_URL"].rstrip("/")
    ws_url = os.environ["HERMES_WS_URL"]
    username = os.environ["HERMES_DASHBOARD_AUTH_USERNAME"]
    password = os.environ["HERMES_DASHBOARD_AUTH_PASSWORD"]

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        ticket_response = await client.post(f"{dashboard_url}/api/auth/ws-ticket")
        if ticket_response.status_code in {401, 403}:
            login_response = await client.post(
                f"{dashboard_url}/auth/password-login",
                json={
                    "provider": "basic",
                    "username": username,
                    "password": password,
                    "next": "/",
                },
            )
            login_response.raise_for_status()
            ticket_response = await client.post(f"{dashboard_url}/api/auth/ws-ticket")
        ticket_response.raise_for_status()
        ticket = ticket_response.json().get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise RuntimeError("Dashboard returned no WebSocket ticket")

    separator = "&" if "?" in ws_url else "?"
    authenticated_url = f"{ws_url}{separator}{urlencode({'ticket': ticket})}"
    events: deque[dict] = deque()
    session_id: str | None = None
    async with websockets.connect(authenticated_url, open_timeout=10, close_timeout=5) as socket:
        try:
            created = await rpc(
                socket,
                "session.create",
                {"source": "sillytavern", "hidden": True, "close_on_disconnect": True},
                events,
            )
            session_id = created.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError("Hermes session.create returned no runtime session id")

            # Hermes emits session.info only after the background AIAgent build is complete.
            # session.create schedules that build after its reply; this event is the readiness signal.
            await wait_for_agent(socket, session_id, events)
            result = await rpc(socket, "tools.show", {"session_id": session_id}, events)
            if result.get("total") != 0 or result.get("sections") != []:
                raise RuntimeError("The built default-profile session exposes tool schemas")
            closed = await rpc(socket, "session.close", {"session_id": session_id}, events)
            if closed.get("closed") is not True:
                raise RuntimeError("Hermes did not close the disposable session")
            session_id = None
            print("PASS: disposable built default-profile session returned total=0 and sections=[]")
            return 0
        finally:
            if session_id:
                try:
                    await rpc(socket, "session.close", {"session_id": session_id}, events)
                except Exception:
                    # Do not replace the original schema/build failure, and never print gateway payloads.
                    pass


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(
            f"FAIL: {type(exc).__name__} (details omitted to avoid printing credentials)",
            file=sys.stderr,
        )
        raise SystemExit(1)
