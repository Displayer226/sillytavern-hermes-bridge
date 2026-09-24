from __future__ import annotations

import json

import httpx
import pytest

from proxy_st.hermes_auth import (
    HermesDashboardAuthenticator,
    HermesDashboardPasswordAuth,
    dashboard_url_join,
)


def test_dashboard_url_join_preserves_path_prefix() -> None:
    assert (
        dashboard_url_join("http://hermes.example.test/dashboard/", "/api/auth/ws-ticket")
        == "http://hermes.example.test/dashboard/api/auth/ws-ticket"
    )


@pytest.mark.asyncio
async def test_fetch_ws_ticket_logs_in_and_retries_with_cookie() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/auth/ws-ticket" and len(requests) == 1:
            return httpx.Response(401, json={"detail": "Unauthorized"})
        if request.url.path == "/auth/password-login":
            assert json.loads(request.content) == {
                "provider": "basic",
                "username": "proxy-user",
                "password": "proxy-pass",
                "next": "/",
            }
            return httpx.Response(
                200,
                json={"ok": True, "next": "/"},
                headers={"set-cookie": "hermes_session=abc123; Path=/; HttpOnly"},
            )
        if request.url.path == "/api/auth/ws-ticket":
            assert "hermes_session=abc123" in request.headers.get("cookie", "")
            return httpx.Response(200, json={"ticket": "ticket-123", "ttl_seconds": 30})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    authenticator = HermesDashboardAuthenticator(
        "http://hermes.example.test/",
        auth=HermesDashboardPasswordAuth(
            provider="basic",
            username="proxy-user",
            password="proxy-pass",
        ),
        client=client,
    )

    try:
        ticket = await authenticator.fetch_ws_ticket()
    finally:
        await authenticator.close()
        await client.aclose()

    assert ticket == "ticket-123"
    assert [request.url.path for request in requests] == [
        "/api/auth/ws-ticket",
        "/auth/password-login",
        "/api/auth/ws-ticket",
    ]
