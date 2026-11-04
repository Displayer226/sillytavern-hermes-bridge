"""Authentication mode, status and failure contracts with synthetic credentials."""

import asyncio
import json
import logging
import os
from types import SimpleNamespace

import httpx
import pytest

from proxy_st import app as app_module
from proxy_st.hermes_auth import DashboardAuthError, HermesDashboardAuthenticator, HermesDashboardPasswordAuth
from proxy_st.hermes_oidc import load_tokens, save_tokens
from proxy_st.hermes_ws import HermesWebSocketManager
from proxy_st import hermes_ws


BASE = "http://hermes.invalid/"
TOKENS = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
          "provider": "self-hosted", "expires_at": 1.0}


def test_manager_initialization_does_not_log_sensitive_urls(caplog):
    with caplog.at_level(logging.INFO):
        HermesWebSocketManager(ws_url="ws://synthetic-user:synthetic-secret@hermes.invalid/api/ws?token=synthetic-secret",
                               dashboard_url="http://hermes.invalid/?token=synthetic-secret")
    assert "synthetic-secret" not in caplog.text


def test_failed_atomic_replace_preserves_previous_credentials(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    save_tokens(path, TOKENS, BASE)
    original = path.read_bytes()
    def fail_replace(source, destination):
        raise OSError("synthetic failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        save_tokens(path, {**TOKENS, "refresh_token": "new"}, BASE)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["password", "auto"])
async def test_password_path_without_oidc_file(tmp_path, mode):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ticket": "synthetic-ticket"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, auth=HermesDashboardPasswordAuth(username="u", password="p"))
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode=mode,
            dashboard_auth_username="u", dashboard_auth_password="p",
            dashboard_oidc_token_file=str(tmp_path / "missing"), dashboard_authenticator=auth)
        assert "ticket=synthetic-ticket" in await manager._authenticated_ws_url()
    assert requests == ["/api/auth/ws-ticket", "/auth/password-login", "/api/auth/ws-ticket"]


@pytest.mark.asyncio
async def test_auto_rejected_oidc_does_not_fallback_to_password(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, TOKENS, BASE)
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(401)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="auto",
            dashboard_auth_username="u", dashboard_auth_password="p", dashboard_oidc_token_file=str(path),
            dashboard_authenticator=HermesDashboardAuthenticator(BASE, client=client))
        with pytest.raises(ConnectionError):
            await manager._authenticated_ws_url()
        assert manager.auth_login_required
        with pytest.raises(ConnectionError):
            await manager._authenticated_ws_url()
    assert requests == ["/auth/native/refresh"]


@pytest.mark.asyncio
async def test_oidc_with_empty_token_path_never_attempts_password():
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"ticket": "synthetic-ticket"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc",
            dashboard_auth_username="u", dashboard_auth_password="p", dashboard_oidc_token_file="",
            dashboard_authenticator=HermesDashboardAuthenticator(BASE, client=client))
        with pytest.raises(ConnectionError):
            await manager._authenticated_ws_url()
        assert manager.auth_login_required
    assert not requests


@pytest.mark.asyncio
async def test_oidc_without_dashboard_never_connects_anonymous(tmp_path):
    manager = HermesWebSocketManager(dashboard_url="", dashboard_auth_mode="oidc",
                                     dashboard_oidc_token_file=str(tmp_path / "missing"))
    with pytest.raises(ConnectionError):
        await manager._authenticated_ws_url()


@pytest.mark.asyncio
async def test_auto_falls_back_to_legacy_after_password_failure(tmp_path, monkeypatch):
    async def legacy(_url):
        return "synthetic-legacy"
    monkeypatch.setattr(hermes_ws, "_fetch_session_token", legacy)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="auto",
            dashboard_auth_username="u", dashboard_auth_password="p", dashboard_oidc_token_file=str(tmp_path / "missing"),
            dashboard_authenticator=HermesDashboardAuthenticator(BASE, client=client))
        assert "token=synthetic-legacy" in await manager._authenticated_ws_url()


@pytest.mark.asyncio
async def test_operator_login_cannot_be_overwritten_by_inflight_refresh(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, TOKENS, BASE)
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.url.path.endswith("/refresh"):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={**TOKENS, "refresh_token": "rotated", "expires_at": 9999999999})
        return httpx.Response(200, json={"ticket": "synthetic-ticket"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        fetch = asyncio.create_task(auth.fetch_ws_ticket())
        await asyncio.wait_for(entered.wait(), 1)
        store = asyncio.create_task(auth.store_oidc_session(
            {**TOKENS, "refresh_token": "operator-new", "expires_at": 9999999999}, str(path)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(fetch, store)
    assert load_tokens(path, BASE)["refresh_token"] == "operator-new"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [400, 403, 429, 503, "timeout"])
async def test_temporary_refresh_failure_keeps_credentials(tmp_path, failure):
    path = tmp_path / "tokens.json"
    save_tokens(path, TOKENS, BASE)
    original = path.read_bytes()
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-sensitive-detail", request=request)
        return httpx.Response(failure, headers={"Retry-After": "10"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc",
            dashboard_oidc_token_file=str(path), dashboard_authenticator=HermesDashboardAuthenticator(BASE, client=client))
        with pytest.raises(ConnectionError) as error:
            await manager._authenticated_ws_url()
        assert "synthetic-sensitive-detail" not in str(error.value)
        assert not manager.auth_login_required
        assert path.read_bytes() == original
        if failure != "timeout":
            assert manager.auth_retry_after == 10


@pytest.mark.asyncio
async def test_password_error_does_not_include_response_or_exception_secret():
    def handler(request):
        return httpx.Response(500, json={"detail": "synthetic-secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client)
        with pytest.raises(DashboardAuthError) as error:
            await auth.fetch_ws_ticket()
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,connected,ready,login,retry,expected", [
    ("password", False, False, False, 0, False),
    ("oidc", False, False, True, 0, True),
    ("oidc", False, False, False, 123, False),
    ("password", True, False, False, 0, False),
    ("password", True, True, False, 0, False),
])
async def test_status_reports_actual_auth_and_gateway_state(monkeypatch, mode, connected, ready, login, retry, expected):
    manager = SimpleNamespace(dashboard_auth_mode=mode, is_connected=connected, is_ready=ready,
        auth_login_required=login, auth_retry_at=retry, _has_oidc_session=lambda: False)
    monkeypatch.setattr(app_module, "hermes_ws_manager", manager)
    monkeypatch.setattr(app_module, "WS_TOKEN", "synthetic-proxy-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://test") as client:
        response = await client.get("/v1/hermes/auth/status", headers={"Authorization": "Bearer synthetic-proxy-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["login_required"] is expected
    assert body["connected"] is connected
    assert body["ready"] is ready
    assert body["retry_at"] == (retry or None)
    assert "synthetic-proxy-token" not in json.dumps(body)


@pytest.mark.asyncio
async def test_stop_waits_for_reconnect_task_cleanup():
    manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc")
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def connect():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    manager._do_connect_and_wait = connect
    await manager.start()
    await asyncio.wait_for(started.wait(), 1)
    await manager.stop()
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_reconnect_exponential_backoff_is_bounded():
    manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc")
    waits = []
    async def connect():
        raise ConnectionError("synthetic failure")
    async def wait(delay):
        waits.append(delay)
        if len(waits) == 12:
            manager._should_connect = False
    manager._do_connect_and_wait = connect
    manager._wait_for_auth = wait
    manager._should_connect = True
    await manager._reconnect_loop()
    assert waits == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]


@pytest.mark.asyncio
async def test_stop_closes_socket_even_if_listener_clears_reference():
    manager = HermesWebSocketManager(dashboard_url=BASE)
    closed = asyncio.Event()
    started = asyncio.Event()
    class Socket:
        async def close(self):
            closed.set()
    manager._ws = Socket()
    async def listen():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            manager._ws = None
    manager._listen_task = asyncio.create_task(listen())
    await started.wait()
    await manager.stop()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_ticket_rejection_after_refresh_is_not_proof_refresh_is_revoked(tmp_path):
    path = tmp_path / "tokens.json"
    save_tokens(path, TOKENS, BASE)
    def handler(request):
        if request.url.path.endswith("/refresh"):
            return httpx.Response(200, json={**TOKENS, "refresh_token": "rotated", "expires_at": 9999999999})
        return httpx.Response(403)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        with pytest.raises(DashboardAuthError):
            await auth.fetch_ws_ticket()
    assert load_tokens(path, BASE)["refresh_token"] == "rotated"
