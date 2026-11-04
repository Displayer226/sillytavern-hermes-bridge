"""Health and proxy authentication remain independent of Hermes readiness."""

from types import SimpleNamespace

import httpx
import pytest

from proxy_st import app as app_module
from proxy_st import health


@pytest.mark.asyncio
async def test_disconnected_backend_keeps_diagnostics_and_auth(monkeypatch):
    monkeypatch.setattr(app_module, "WS_TOKEN", "synthetic-proxy-token")
    monkeypatch.setattr(app_module, "hermes_ws_manager", SimpleNamespace(
        is_connected=False, is_ready=False, session_count=0,
    ))
    monkeypatch.setattr(health, "MODELS_FETCH_BACKENDS", False)
    monkeypatch.setitem(health.BACKEND_CONFIGS["hermes"], "ws_url", "ws://unreachable.invalid/api/ws")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        denied = await client.get("/v1/session/synthetic/tool_calls")
        allowed = await client.get("/v1/session/synthetic/tool_calls", headers={"Authorization": "Bearer synthetic-proxy-token"})
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["backends"]["hermes"]["websocket_ready"] is False
    assert denied.status_code == 401
    assert allowed.status_code == 200
