from __future__ import annotations

import httpx
import pytest

from proxy_st import app as app_module
from proxy_st.auth import is_worker_voice_path


@pytest.mark.asyncio
async def test_health_stays_public_when_proxy_token_is_configured(monkeypatch):
    monkeypatch.setattr(app_module, "WS_TOKEN", "secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_http_routes_require_proxy_token_when_configured(monkeypatch):
    monkeypatch.setattr(app_module, "WS_TOKEN", "secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app),
        base_url="http://test",
    ) as client:
        missing = await client.get("/v1/session/chat-1/tool_calls")
        allowed = await client.get(
            "/v1/session/chat-1/tool_calls",
            headers={"Authorization": "Bearer secret"},
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "proxy_auth_required"
    assert allowed.status_code == 200
    assert allowed.json()["tool_calls"] == []


def test_voice_worker_path_exemption_is_scoped_to_worker_callbacks():
    assert is_worker_voice_path("/v1/voice/calls/call-1/context") is True
    assert is_worker_voice_path("/v1/voice/calls/call-1/completions") is True
    assert is_worker_voice_path("/v1/voice/calls") is False
    assert is_worker_voice_path("/v1/voice/calls/call-1") is False


@pytest.mark.asyncio
async def test_proxy_token_header_still_authenticates(monkeypatch):
    """Redacting x-proxy-token in logs must not change its auth role."""
    monkeypatch.setattr(app_module, "WS_TOKEN", "secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app),
        base_url="http://test",
    ) as client:
        denied = await client.get(
            "/v1/session/chat-1/tool_calls",
            headers={"X-Proxy-Token": "wrong"},
        )
        allowed = await client.get(
            "/v1/session/chat-1/tool_calls",
            headers={"X-Proxy-Token": "secret"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["tool_calls"] == []
