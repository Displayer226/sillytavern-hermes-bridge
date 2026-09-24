from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from .log import logger
from .realtime import ws_broadcast
from .state import WS_CONNECTIONS
from .utils import now_iso


MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_SERVER_SESSION_ID = "sillytavern-session-proxy"

PERSONA_TOOL_NAME = "sillytavern_update_persona_description"


PERSONA_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "The current SillyTavern chat/session id. Use the session_id from the SillyTavern integration context.",
        },
        "description": {
            "type": "string",
            "description": (
                "Persona text to apply. With operation=append/prepend, this is only the text to add. "
                "With operation=replace, this must be the complete final character description/persona field."
            ),
        },
        "content": {
            "type": "string",
            "description": "Alias for description. Prefer description when possible.",
        },
        "text": {
            "type": "string",
            "description": "Alias for description. Prefer description when possible.",
        },
        "operation": {
            "type": "string",
            "enum": ["auto", "append", "prepend", "replace"],
            "description": (
                "How to apply description. Use append for small additions or tests, prepend for front matter, "
                "replace only when providing the complete final persona. Defaults to auto; auto appends short additions."
            ),
        },
        "summary": {
            "type": "string",
            "description": "Short human-readable summary of what changed.",
        },
        "reason": {
            "type": "string",
            "description": "Why this persona update is appropriate.",
        },
        "require_approval": {
            "type": "boolean",
            "description": "When true, the extension must wait for explicit user approval before applying the update.",
        },
        "confirm_replace": {
            "type": "boolean",
            "description": "Set true only when intentionally replacing a long persona with a much shorter complete persona.",
        },
    },
    "required": ["summary"],
    "additionalProperties": False,
}


PERSONA_TOOL_DEFINITION: dict[str, Any] = {
    "name": PERSONA_TOOL_NAME,
    "description": (
        "Propose or apply an update to the native SillyTavern character description/persona field. "
        "This changes only the active character description in SillyTavern, with extension-side version history and user approval. "
        "This is the correct tool for persona updates; memory and skill tools do not change SillyTavern's visible persona field. "
        "Put persona text in description, content, or text. Use operation=append for small additions/tests, "
        "and operation=replace only when providing the full final persona. "
        "In Hermes, call the native tool directly as mcp_sillytavern_sillytavern_update_persona_description; "
        "use terminal/curl only as a fallback if the native tool is unavailable."
    ),
    "inputSchema": PERSONA_TOOL_SCHEMA,
}


class FrontendToolBroker:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def resolve_session_id(self, arguments: dict[str, Any]) -> str:
        explicit = str(arguments.get("session_id") or "").strip()
        if explicit:
            return explicit

        connected_sessions = [session_id for session_id, sockets in WS_CONNECTIONS.items() if sockets]
        if len(connected_sessions) == 1:
            return connected_sessions[0]

        raise ValueError(
            "Missing session_id. Pass the current SillyTavern session_id from the integration context."
        )

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        session_id = self.resolve_session_id(arguments)
        connections = WS_CONNECTIONS.get(session_id) or []
        if not connections:
            raise RuntimeError(f"No SillyTavern extension websocket is subscribed for session_id={session_id!r}")

        request_id = f"mcp-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        try:
            operation = str(arguments.get("operation") or "auto").strip() or "auto"
            logger.info(
                "MCP frontend tool request dispatch tool=%s session=%s request_id=%s operation=%s",
                tool_name,
                session_id,
                request_id,
                operation,
            )
            await ws_broadcast(session_id, {
                "type": "frontend_tool_request",
                "request_id": request_id,
                "tool_name": tool_name,
                "session_id": session_id,
                "arguments": arguments,
                "created_at": now_iso(),
            })
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.info(
                "MCP frontend tool request complete tool=%s session=%s request_id=%s status=%s operation=%s",
                tool_name,
                session_id,
                request_id,
                result.get("status"),
                operation,
            )
            return result
        finally:
            self._pending.pop(request_id, None)

    def complete(self, request_id: str, payload: dict[str, Any]) -> bool:
        future = self._pending.get(request_id)
        if not future:
            return False
        if not future.done():
            future.set_result(payload)
        return True


frontend_tool_broker = FrontendToolBroker()


def mcp_response_headers() -> dict[str, str]:
    return {
        MCP_SESSION_HEADER: MCP_SERVER_SESSION_ID,
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }


def _json_rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _json_rpc_error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _tool_text_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ],
        "isError": is_error,
    }


def _initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": "sillytavern-session-proxy",
            "version": "0.1.0",
        },
        "instructions": (
            "Use these tools only for SillyTavern UI or character-card actions requested by the user. "
            "For persona updates, pass the current SillyTavern session_id from the integration context. "
            "Use operation=append for small additions and operation=replace only for complete persona rewrites."
        ),
    }


async def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(params.get("name") or "").strip()
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    if tool_name != PERSONA_TOOL_NAME:
        return _tool_text_result({"status": "error", "message": f"Unknown tool: {tool_name}"}, is_error=True)

    description = str(arguments.get("description") or arguments.get("content") or arguments.get("text") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    if not description:
        return _tool_text_result({"status": "error", "message": "description, content, or text is required"}, is_error=True)
    if not summary:
        return _tool_text_result({"status": "error", "message": "summary is required"}, is_error=True)

    try:
        result = await frontend_tool_broker.call(tool_name, arguments)
    except asyncio.TimeoutError:
        logger.warning("MCP frontend tool request timed out tool=%s", tool_name)
        return _tool_text_result({"status": "error", "message": "Timed out waiting for SillyTavern extension response"}, is_error=True)
    except Exception as exc:
        logger.warning("MCP frontend tool request failed tool=%s: %s", tool_name, exc)
        return _tool_text_result({"status": "error", "message": str(exc)}, is_error=True)

    status = str(result.get("status") or "").lower()
    return _tool_text_result(result, is_error=status == "error")


async def handle_mcp_json_rpc(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _json_rpc_error(None, -32600, "Invalid Request")

    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    is_notification = "id" not in message
    try:
        if method == "initialize":
            return _json_rpc_result(request_id, _initialize_result())

        if method == "notifications/initialized":
            return None if is_notification else _json_rpc_result(request_id, {})

        if method == "ping":
            return None if is_notification else _json_rpc_result(request_id, {})

        if method == "tools/list":
            return _json_rpc_result(request_id, {"tools": [PERSONA_TOOL_DEFINITION]})

        if method == "tools/call":
            return _json_rpc_result(request_id, await _call_tool(params))

        if method in {"resources/list", "prompts/list"}:
            key = "resources" if method == "resources/list" else "prompts"
            return _json_rpc_result(request_id, {key: []})

        return None if is_notification else _json_rpc_error(request_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        logger.exception("MCP request failed method=%s", method)
        return None if is_notification else _json_rpc_error(request_id, -32603, "Internal error", {"message": str(exc)})


async def handle_mcp_http_payload(payload: Any) -> tuple[int, Any | None]:
    if isinstance(payload, list):
        responses = [response for response in [await handle_mcp_json_rpc(item) for item in payload] if response is not None]
        if not responses:
            return 202, None
        return 200, responses

    response = await handle_mcp_json_rpc(payload)
    if response is None:
        return 202, None
    return 200, response


async def mcp_sse_keepalive():
    yield ": sillytavern-session-proxy mcp stream\n\n"
    while True:
        await asyncio.sleep(15)
        yield ": keepalive\n\n"
