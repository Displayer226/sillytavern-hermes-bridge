from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlencode

import httpx
import pytest

from proxy_st.hermes_auth import DashboardAuthError, HermesDashboardAuthenticator
from proxy_st.hermes_oidc import OIDCLoginRequired, callback_code, load_tokens, save_tokens
from proxy_st.hermes_ws import HermesWebSocketManager

BASE = "http://hermes.test/prefix/"


def tokens(expiry: float | None = None, suffix: str = "old") -> dict:
    return {"access_token": f"access-{suffix}", "refresh_token": f"refresh-{suffix}",
            "provider": "self-hosted", "expires_at": expiry if expiry is not None else time.time() + 3600}


@pytest.mark.asyncio
async def test_oidc_refresh_rotation_and_concurrent_tickets(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(0), BASE)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/auth/native/refresh"):
            assert json.loads(request.content) == {"provider": "self-hosted", "refresh_token": "refresh-old"}
            return httpx.Response(200, json=tokens(suffix="new"))
        assert request.url.path == "/prefix/api/auth/ws-ticket"
        assert request.headers["Authorization"] == "Bearer access-new"
        # Rotation is durable BEFORE using the new token.
        assert load_tokens(path, BASE)["refresh_token"] == "refresh-new"
        return httpx.Response(200, json={"ticket": "single-use"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        assert await asyncio.gather(auth.fetch_ws_ticket(), auth.fetch_ws_ticket()) == ["single-use", "single-use"]
    assert len(requests) == 3
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_status,exception", [(401, OIDCLoginRequired), (503, DashboardAuthError)])
async def test_rejected_access_refresh_failure_retains_credentials(tmp_path: Path, refresh_status: int, exception: type) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(), BASE)
    original = path.read_bytes()
    routes = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.append(request.url.path)
        return httpx.Response(refresh_status if request.url.path.endswith("/refresh") else 401,
                              json={"detail": "do-not-leak-access-or-refresh-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        with pytest.raises(exception) as error:
            await auth.fetch_ws_ticket()
        assert "do-not-leak" not in str(error.value)
    if refresh_status == 503:
        assert path.read_bytes() == original
    else:
        assert json.loads(path.read_text())["refresh_token"] == "refresh-old"
        with pytest.raises(OIDCLoginRequired):
            load_tokens(path, BASE)
    assert routes == ["/prefix/api/auth/ws-ticket", "/prefix/auth/native/refresh"]


@pytest.mark.asyncio
async def test_rejected_access_refreshes_once_and_retries(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(), BASE)
    routes = []

    def handler(request: httpx.Request) -> httpx.Response:
        routes.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return httpx.Response(200, json=tokens(suffix="new"))
        if request.headers["Authorization"] == "Bearer access-old":
            return httpx.Response(401)
        return httpx.Response(200, json={"ticket": "fresh-ticket"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        assert await auth.fetch_ws_ticket() == "fresh-ticket"
    assert len(routes) == 3


@pytest.mark.asyncio
async def test_missing_session_requires_login_without_network(tmp_path: Path) -> None:
    manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc",
                                     dashboard_oidc_token_file=str(tmp_path / "missing"))
    try:
        with pytest.raises(ConnectionError, match="OIDC login required"):
            await manager._authenticated_ws_url()
    finally:
        await manager._dashboard_authenticator.close()


def test_file_is_bound_to_dashboard_and_private(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(), BASE)
    with pytest.raises(DashboardAuthError, match="different dashboard"):
        load_tokens(path, "https://other.test")
    path.chmod(0o644)
    with pytest.raises(DashboardAuthError, match="0600"):
        load_tokens(path, BASE)


@pytest.mark.parametrize("query", [
    {"code": "code", "state": "wrong"}, {"state": "state"},
    {"code": "code", "state": "state", "error": "denied"},
])
def test_callback_rejects_invalid_state_or_error(query: dict) -> None:
    redirect = "http://127.0.0.1:12345/callback"
    with pytest.raises(DashboardAuthError):
        callback_code(redirect + "?" + urlencode(query), redirect, "state")


def test_callback_checks_address() -> None:
    redirect = "http://127.0.0.1:12345/callback"
    assert callback_code(redirect + "?code=one-use&state=state", redirect, "state") == "one-use"
    with pytest.raises(DashboardAuthError):
        callback_code("http://evil.test/callback?code=c&state=state", redirect, "state")


@pytest.mark.asyncio
async def test_auto_prefers_saved_oidc_over_password(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(), BASE)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/prefix/api/auth/ws-ticket"
        assert request.headers["Authorization"] == "Bearer access-old"
        return httpx.Response(200, json={"ticket": "oidc-ticket"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="auto",
                                         dashboard_auth_username="user", dashboard_auth_password="pass",
                                         dashboard_oidc_token_file=str(path), dashboard_authenticator=auth)
        assert "ticket=oidc-ticket" in await manager._authenticated_ws_url()


def test_manual_login_pkce_uses_internal_exchange_and_runtime_binding(tmp_path: Path, monkeypatch) -> None:
    import argparse
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlsplit

    from proxy_st.hermes_oidc import login

    captured = {}
    path = tmp_path / "credentials.json"
    original_client = httpx.Client

    def display(message: str, **kwargs) -> None:
        if message.startswith("Open this URL"):
            captured.update(parse_qs(urlsplit(message.split("\n", 1)[1]).query))

    def callback(prompt: str) -> str:
        return captured["redirect_uri"][0] + "?" + urlencode({"state": captured["state"][0], "code": "one-use-code"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://dashboard-internal:9120/auth/native/token"
        body = json.loads(request.content)
        assert body["code"] == "one-use-code"
        expected = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert expected == captured["code_challenge"][0]
        return httpx.Response(200, json=tokens())

    monkeypatch.setattr("builtins.print", display)
    monkeypatch.setattr("builtins.input", callback)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    login(argparse.Namespace(public_url="https://public.test/hermes/", exchange_url="http://dashboard-internal:9120/",
                             dashboard_url=BASE, port=0, provider="self-hosted", token_file=str(path), manual=True))
    assert load_tokens(path, BASE)["provider"] == "self-hosted"


@pytest.mark.asyncio
async def test_auto_picks_up_new_session_after_password_client_created(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ticket": "ticket"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client)
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="auto",
                                         dashboard_oidc_token_file=str(path), dashboard_authenticator=auth)
        await manager._fetch_ws_ticket()
        assert auth.oidc_token_file == ""
        save_tokens(path, tokens(), BASE)
        await manager._authenticated_ws_url()
        assert auth.oidc_token_file == str(path)


@pytest.mark.asyncio
async def test_expired_session_stops_requests_across_restart_and_login_wakes_manager(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(0), BASE)
    requests = []
    rejected, connected = asyncio.Event(), asyncio.Event()

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/refresh"):
            rejected.set()
            return httpx.Response(401)
        return httpx.Response(200, json={"ticket": "renewed-ticket"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc",
                                         dashboard_oidc_token_file=str(path), dashboard_authenticator=auth)

        async def connect():
            await manager._authenticated_ws_url()
            connected.set()
            await asyncio.Event().wait()

        manager._do_connect_and_wait = connect
        await manager.start()
        try:
            await asyncio.wait_for(rejected.wait(), 1)
            await asyncio.sleep(0.02)
            assert manager.auth_login_required
            # A newly created client after a restart must not reuse the dead token.
            restarted = HermesDashboardAuthenticator(BASE, client=client, oidc_token_file=str(path))
            with pytest.raises(OIDCLoginRequired):
                await restarted.fetch_ws_ticket()
            assert len(requests) == 1
            await manager.store_oidc_session(tokens(suffix="new"))
            await asyncio.wait_for(connected.wait(), 1)
            assert not manager.auth_login_required
            assert len(requests) == 2
            assert "login_required" not in json.loads(path.read_text())
        finally:
            task = manager._reconnect_task
            await manager.stop()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["120", "Wed, 04 Nov 2026 10:02:00 GMT"])
async def test_refresh_retry_after_survives_ticket_error(tmp_path: Path, monkeypatch, header: str) -> None:
    from proxy_st import hermes_auth
    monkeypatch.setattr(hermes_auth.time, "time", lambda: 1793786400.0)  # 10:00 UTC
    path = tmp_path / "tokens.json"
    save_tokens(path, tokens(0), BASE)
    def handler(request):
        return httpx.Response(503, headers={"Retry-After": header})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc",
            dashboard_oidc_token_file=str(path), dashboard_authenticator=HermesDashboardAuthenticator(BASE, client=client))
        with pytest.raises(ConnectionError):
            await manager._authenticated_ws_url()
        assert manager.auth_retry_after == 120
        assert not manager.auth_login_required
        assert load_tokens(path, BASE)["refresh_token"] == "refresh-old"


@pytest.mark.asyncio
async def test_reconnect_respects_provider_delay_and_login_interrupts_backoff(tmp_path: Path) -> None:
    manager = HermesWebSocketManager(dashboard_url=BASE, dashboard_auth_mode="oidc",
                                     dashboard_oidc_token_file=str(tmp_path / "tokens.json"))
    waits = []
    async def connect():
        manager.auth_retry_after = 1800
        raise ConnectionError("rate limited")
    async def wait(delay):
        waits.append(delay)
        manager._should_connect = False
    manager._do_connect_and_wait = connect
    manager._wait_for_auth = wait
    manager._should_connect = True
    await manager._reconnect_loop()
    assert waits == [1800]
    # Browser sign-in interrupts a long wait instead of waiting for its timeout.
    manager._wait_for_auth = HermesWebSocketManager._wait_for_auth.__get__(manager)
    await manager.store_oidc_session(tokens())
    await asyncio.wait_for(manager._wait_for_auth(1800), 1)
    await manager.stop()
