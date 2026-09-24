from __future__ import annotations

import asyncio
from typing import Any

from proxy_st import mcp


def test_mcp_initialize_returns_tool_capability() -> None:
    response = asyncio.run(mcp.handle_mcp_json_rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"},
    }))

    assert response is not None
    assert response["id"] == 1
    assert response["result"]["capabilities"]["tools"]["listChanged"] is False


def test_mcp_tools_list_exposes_persona_tool() -> None:
    response = asyncio.run(mcp.handle_mcp_json_rpc({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    }))

    tools = response["result"]["tools"]
    assert tools[0]["name"] == mcp.PERSONA_TOOL_NAME
    assert "memory and skill tools do not change SillyTavern" in tools[0]["description"]
    assert tools[0]["inputSchema"]["required"] == ["summary"]
    assert "description" in tools[0]["inputSchema"]["properties"]
    assert "content" in tools[0]["inputSchema"]["properties"]
    assert tools[0]["inputSchema"]["properties"]["operation"]["enum"] == ["auto", "append", "prepend", "replace"]


def test_mcp_persona_tool_routes_to_frontend(monkeypatch) -> None:
    async def fake_call(tool_name: str, arguments: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        assert tool_name == mcp.PERSONA_TOOL_NAME
        assert arguments["session_id"] == "chat-1"
        assert timeout == 120.0
        return {"status": "applied", "message": "ok"}

    monkeypatch.setattr(mcp.frontend_tool_broker, "call", fake_call)

    response = asyncio.run(mcp.handle_mcp_json_rpc({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": mcp.PERSONA_TOOL_NAME,
            "arguments": {
                "session_id": "chat-1",
                "description": "Updated persona",
                "summary": "Test update",
            },
        },
    }))

    result = response["result"]
    assert result["isError"] is False
    assert '"status": "applied"' in result["content"][0]["text"]


def test_mcp_persona_tool_validates_required_fields() -> None:
    response = asyncio.run(mcp.handle_mcp_json_rpc({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": mcp.PERSONA_TOOL_NAME,
            "arguments": {
                "session_id": "chat-1",
                "summary": "Missing description",
            },
        },
    }))

    result = response["result"]
    assert result["isError"] is True
    assert "description, content, or text is required" in result["content"][0]["text"]


def test_mcp_persona_tool_accepts_content_alias(monkeypatch) -> None:
    async def fake_call(tool_name: str, arguments: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        assert tool_name == mcp.PERSONA_TOOL_NAME
        assert arguments["content"] == "Append this"
        assert arguments["operation"] == "append"
        return {"status": "applied", "message": "ok"}

    monkeypatch.setattr(mcp.frontend_tool_broker, "call", fake_call)

    response = asyncio.run(mcp.handle_mcp_json_rpc({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": mcp.PERSONA_TOOL_NAME,
            "arguments": {
                "session_id": "chat-1",
                "content": "Append this",
                "operation": "append",
                "summary": "Append test",
            },
        },
    }))

    result = response["result"]
    assert result["isError"] is False
    assert '"status": "applied"' in result["content"][0]["text"]


def test_mcp_initialized_notification_returns_no_body() -> None:
    status, payload = asyncio.run(mcp.handle_mcp_http_payload({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }))

    assert status == 202
    assert payload is None
