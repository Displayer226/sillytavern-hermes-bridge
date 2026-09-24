from typing import Any

from fastapi import WebSocket


RATE_LIMIT_TRACKER: dict[str, list[float]] = {}
SESSION_TOOL_CALLS: dict[str, list[dict[str, Any]]] = {}
SESSION_INFOS: dict[str, dict[str, Any]] = {}

WS_CONNECTIONS: dict[str, list[WebSocket]] = {}
WS_SUBSCRIPTIONS: dict[WebSocket, set[str]] = {}
