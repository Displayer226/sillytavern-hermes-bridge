"""Tests for optional Hermes HTTP model discovery readiness."""
from unittest.mock import AsyncMock

import httpx
import pytest

from proxy_st import app as app_module
from proxy_st import health as health_module
from proxy_st import models as models_module
from proxy_st.config import BACKEND_CONFIGS


def _no_http_client(*_args, **_kwargs):
    raise AssertionError("HTTP model discovery must not run when disabled")


def _set_ws_state(monkeypatch, ready: bool) -> None:
    manager_class = type(app_module.hermes_ws_manager)
    monkeypatch.setattr(manager_class, "is_ready", property(lambda _self: ready))
    monkeypatch.setattr(manager_class, "is_connected", property(lambda _self: ready))
    monkeypatch.setattr(manager_class, "session_count", property(lambda _self: 1 if ready else 0))


class _UnreachableClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, *_args, **_kwargs):
        raise httpx.ConnectError("connection refused")


@pytest.mark.asyncio
async def test_disabled_discovery_ready_websocket_skips_http_probe(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "MODELS_FETCH_BACKENDS", False)
    monkeypatch.setattr(health_module.httpx, "AsyncClient", _no_http_client)
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "http://internal.invalid")
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://internal.invalid/ws")
    _set_ws_state(monkeypatch, True)
    snapshot = await health_module.readiness_snapshot(app_module.hermes_ws_manager)
    assert snapshot["ready"] is True
    assert snapshot["backends"]["hermes"]["status"] == "ready"


@pytest.mark.asyncio
async def test_unready_websocket_returns_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "MODELS_FETCH_BACKENDS", False)
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://internal.invalid/ws")
    _set_ws_state(monkeypatch, False)
    snapshot = await health_module.readiness_snapshot(app_module.hermes_ws_manager)
    assert snapshot["ready"] is False
    assert snapshot["backends"]["hermes"]["status"] == "not_ready"


@pytest.mark.asyncio
async def test_enabled_discovery_unreachable_http_returns_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "MODELS_FETCH_BACKENDS", True)
    monkeypatch.setattr(health_module.httpx, "AsyncClient", _UnreachableClient)
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "http://internal.invalid")
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://internal.invalid/ws")
    _set_ws_state(monkeypatch, True)
    snapshot = await health_module.readiness_snapshot(app_module.hermes_ws_manager)
    assert snapshot["ready"] is False
    assert snapshot["backends"]["hermes"]["status"] == "unreachable"


@pytest.mark.asyncio
async def test_models_include_static_hermes_without_base_url(monkeypatch) -> None:
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "")
    monkeypatch.setattr(models_module, "MODELS_FETCH_BACKENDS", False)
    monkeypatch.setattr(models_module, "MODELS", ["hermes"])
    fetch = AsyncMock()
    monkeypatch.setattr(models_module, "fetch_backend_models", fetch)
    models_module.clear_models_cache()
    result = await models_module.list_proxy_models(refresh=True)
    assert [model["id"] for model in result["data"]] == ["hermes"]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_payload_redacts_probe_details(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "MODELS_FETCH_BACKENDS", True)
    monkeypatch.setattr(health_module.httpx, "AsyncClient", _UnreachableClient)
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "http://internal.invalid/secret")
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://internal.invalid/ws")
    _set_ws_state(monkeypatch, True)
    snapshot = await health_module.readiness_snapshot(app_module.hermes_ws_manager)
    hermes = snapshot["backends"]["hermes"]
    assert hermes["error_type"] == "http_error"
    assert "url" not in hermes
    assert "error" not in hermes
