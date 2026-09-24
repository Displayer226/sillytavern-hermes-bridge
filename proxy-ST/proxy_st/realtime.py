import json
from typing import Any

from fastapi import WebSocket

from .state import WS_CONNECTIONS, WS_SUBSCRIPTIONS


def _remove_dead_ws(ws: WebSocket) -> None:
    """Remove a dead WebSocket from all connection lists and subscriptions."""
    subs = WS_SUBSCRIPTIONS.pop(ws, None)
    if not subs:
        # Still try to remove from any connection lists it might be in
        for session_id in list(WS_CONNECTIONS):
            connections = WS_CONNECTIONS.get(session_id, [])
            if ws in connections:
                connections.remove(ws)
            if not connections:
                WS_CONNECTIONS.pop(session_id, None)
        return
    for session_id in subs:
        connections = WS_CONNECTIONS.get(session_id, [])
        if ws in connections:
            connections.remove(ws)
        if not connections:
            WS_CONNECTIONS.pop(session_id, None)


async def ws_broadcast(session_id: str, message: dict[str, Any]) -> None:
    connections = WS_CONNECTIONS.get(session_id, [])
    if not connections:
        return
    payload = json.dumps(message, ensure_ascii=False, default=str)
    dead: list[WebSocket] = []
    for ws in connections:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _remove_dead_ws(ws)


def ws_subscribe(ws: WebSocket, session_id: str) -> None:
    if session_id not in WS_CONNECTIONS:
        WS_CONNECTIONS[session_id] = []
    if ws not in WS_CONNECTIONS[session_id]:
        WS_CONNECTIONS[session_id].append(ws)
    if ws not in WS_SUBSCRIPTIONS:
        WS_SUBSCRIPTIONS[ws] = set()
    WS_SUBSCRIPTIONS[ws].add(session_id)


def ws_unsubscribe(ws: WebSocket, session_id: str) -> None:
    connections = WS_CONNECTIONS.get(session_id, [])
    if ws in connections:
        connections.remove(ws)
    if not connections:
        WS_CONNECTIONS.pop(session_id, None)
    subs = WS_SUBSCRIPTIONS.get(ws)
    if subs:
        subs.discard(session_id)


def ws_unsubscribe_all(ws: WebSocket) -> None:
    subs = WS_SUBSCRIPTIONS.pop(ws, None)
    if not subs:
        return
    for session_id in subs:
        connections = WS_CONNECTIONS.get(session_id, [])
        if ws in connections:
            connections.remove(ws)
        if not connections:
            WS_CONNECTIONS.pop(session_id, None)


def ws_is_subscribed(ws: WebSocket, session_id: str) -> bool:
    """Return whether this browser socket owns a subscription for *session_id*."""
    return session_id in WS_SUBSCRIPTIONS.get(ws, set())
