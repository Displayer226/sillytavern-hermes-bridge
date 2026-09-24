from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, APIWebSocketRoute

from proxy_st import app as app_module
from proxy_st import models as models_module
from proxy_st.config import BACKEND_CONFIGS
from proxy_st.logging_utils import request_summary


def _fake_request_entry(request, body, _raw_body):
    return {
        "request_id": "compat-test",
        "summary": request_summary(dict(request.headers), body),
    }


def test_bridge_routes_are_registered() -> None:
    http_routes = {
        (method, route.path)
        for route in app_module.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    websocket_routes = {
        route.path for route in app_module.app.routes if isinstance(route, APIWebSocketRoute)
    }

    assert {"/ws"} <= websocket_routes
    assert {
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/models"),
        ("GET", "/v1/session/{session_id}/tool_calls"),
        ("GET", "/v1/session/{session_id}/info"),
        ("DELETE", "/v1/session/{session_id}"),
        ("GET", "/v1/session/{session_id}/workspace/tree"),
        ("GET", "/v1/session/{session_id}/workspace/file"),
        ("GET", "/v1/session/{session_id}/workspace/download"),
        ("GET", "/v1/hermes/model_options"),
        ("GET", "/v1/hermes/profile_options"),
        ("POST", "/v1/session/{session_id}/model"),
        ("POST", "/v1/session/{session_id}/profile"),
        ("POST", "/mcp"),
        ("GET", "/health/ready"),
    } <= http_routes
    assert ("POST", "/responses") not in http_routes
    assert ("POST", "/v1/responses") not in http_routes


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_backend", [False, True])
async def test_chat_uses_hermes_websocket_without_http_base_url(monkeypatch, explicit_backend) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(app_module, "log_request", AsyncMock(side_effect=_fake_request_entry))
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "")
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://hermes.test/api/ws")
    forward = AsyncMock(return_value=JSONResponse({"routed": "hermes"}))
    monkeypatch.setattr("proxy_st.relay.forward_hermes_streaming", forward)

    metadata = {"session_id": "chat-1"}
    if explicit_backend:
        metadata["backend"] = "hermes"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "Hello"}],
                "st_proxy": metadata,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"routed": "hermes"}
    forward.assert_awaited_once()
    assert forward.await_args.args[1] == "chat-1"


@pytest.mark.asyncio
async def test_unsupported_backend_returns_clear_http_error(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(app_module, "log_request", AsyncMock(side_effect=_fake_request_entry))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "st_proxy": {"session_id": "chat-1", "backend": "unsupported"},
            },
        )
        removed_route = await client.post("/v1/responses", json={"input": "Hello"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_backend"
    assert "Hermes Agent" in response.json()["error"]["message"]
    assert removed_route.status_code == 404
    assert removed_route.json()["error"]["code"] == "unhandled_path"


@pytest.mark.asyncio
async def test_models_endpoint_works_without_http_discovery(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(app_module, "log_request", AsyncMock(side_effect=_fake_request_entry))
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "")
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://hermes.test/api/ws")
    monkeypatch.setattr(models_module, "MODELS", ["hermes"])
    forward = AsyncMock(return_value=JSONResponse({"routed": "hermes"}))
    monkeypatch.setattr("proxy_st.relay.forward_hermes_streaming", forward)
    models_module.clear_models_cache()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/models?refresh=true")
            selected_model = response.json()["data"][0]["id"]
            chat_response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": selected_model,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "st_proxy": {"session_id": "chat-1"},
                },
            )
    finally:
        models_module.clear_models_cache()

    assert response.status_code == 200
    assert response.json()["object"] == "list"
    assert [model["id"] for model in response.json()["data"]] == ["hermes"]
    assert chat_response.status_code == 200
    forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_websocket_model_options_use_hermes_manager(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    model_options = AsyncMock(return_value={"providers": [{"name": "Hermes", "models": ["test-model"]}]})
    monkeypatch.setattr(app_module.hermes_ws_manager, "model_options", model_options)

    class FakeWebSocket:
        headers = {}
        query_params = {}

        def __init__(self):
            self.messages = iter([
                {"type": "ping"},
                {"type": "get_model_options", "session_id": "chat-1"},
            ])
            self.sent = []

        async def accept(self, subprotocol=None):
            return None

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_text(self):
            try:
                return json.dumps(next(self.messages))
            except StopIteration:
                raise WebSocketDisconnect() from None

    websocket = FakeWebSocket()
    await app_module.websocket_endpoint(websocket)

    assert [message["type"] for message in websocket.sent] == ["connected", "pong", "model_options"]
    result = websocket.sent[-1]
    assert result["type"] == "model_options"
    assert result["session_id"] == "chat-1"
    assert result["options"]["providers"][0]["models"] == ["test-model"]
    model_options.assert_awaited_once_with("chat-1")


@pytest.mark.asyncio
async def test_session_info_does_not_carry_interactive_server_request_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(app_module.hermes_ws_manager, "get_session_info", AsyncMock(return_value=None))
    monkeypatch.setattr(app_module.hermes_ws_manager, "session_status", lambda _session_id: "active")
    monkeypatch.setattr(
        app_module.hermes_ws_manager,
        "server_request_snapshot",
        lambda _session_id: [{"rpc_id": "srq-new", "method": "approval"}],
    )

    info = await app_module._combined_session_info("chat-1")

    assert "server_requests" not in info
