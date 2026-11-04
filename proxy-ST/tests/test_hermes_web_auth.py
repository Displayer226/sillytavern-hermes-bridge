from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from proxy_st.hermes_auth import DashboardAuthError
from proxy_st.hermes_web_auth import BrowserOIDC, WebOIDCSettings, web_auth_router

ORIGIN = "https://st.test"
ISSUER = "https://login.test"
SETTINGS = WebOIDCSettings(issuer=ISSUER, client_id="hermes-dashboard", callback_url=ORIGIN + "/hermes-auth/callback")


@pytest.fixture
def browser_flow():
    saved = []
    requests = []
    scenario = {"nonce": None, "token_status": 200, "identity_status": 200, "refresh": "refresh-secret",
                "identity_provider": "self-hosted", "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token"}

    async def save(tokens):
        saved.append(tokens)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": scenario["authorization_endpoint"],
                                            "token_endpoint": scenario["token_endpoint"]})
        if request.url.path == "/token":
            data = parse_qs(request.content.decode())
            pending = list(flow.pending.values())
            assert not pending  # consumed before exchange; cannot be replayed
            verifier = data["code_verifier"][0]
            expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert expected_challenge == scenario["challenge"]
            claims = base64.urlsafe_b64encode(json.dumps({"nonce": scenario["nonce"]}).encode()).rstrip(b"=").decode()
            return httpx.Response(scenario["token_status"], json={"id_token": f"header.{claims}.signature",
                                                                 "refresh_token": scenario["refresh"]})
        if request.url.path == "/api/auth/me":
            assert request.headers["authorization"].startswith("Bearer header.")
            return httpx.Response(scenario["identity_status"], json={"provider": scenario["identity_provider"], "user_id": "operator",
                                                                    "expires_at": time.time() + 3600})
        raise AssertionError(str(request.url))

    flow = BrowserOIDC(SETTINGS, "http://hermes:9119", save,
                       client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    app = FastAPI()
    app.include_router(web_auth_router(flow, lambda: {"ready": False, "login_required": True}))
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, flow, saved, scenario, requests


def begin(client, scenario):
    response = client.post("/v1/hermes/auth/login", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert all(value in cookie for value in ["HttpOnly", "Secure", "SameSite=lax", "Path=/hermes-auth"])
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    scenario["nonce"] = query["nonce"][0]
    scenario["challenge"] = query["code_challenge"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" in query["scope"][0]
    return query["state"][0]


def test_browser_flow_saves_verified_session_and_redirects_without_secrets(browser_flow):
    client, flow, saved, scenario, requests = browser_flow
    state = begin(client, scenario)
    response = client.get("/hermes-auth/callback", params={"state": state, "code": "one-use-secret", "next": "https://evil.test"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == ORIGIN + "/"
    assert response.headers["cache-control"] == "no-store"
    assert saved[0]["refresh_token"] == "refresh-secret"
    assert saved[0]["provider"] == "self-hosted"
    assert not flow.pending
    assert "secret" not in response.text
    replay = client.get("/hermes-auth/callback", params={"state": state, "code": "one-use-secret"}, follow_redirects=False)
    assert replay.status_code == 400
    assert len(saved) == 1
    assert any(request.url.path == "/api/auth/me" for request in requests)


@pytest.mark.parametrize("endpoint", ["authorization_endpoint", "token_endpoint"])
def test_discovery_rejects_foreign_endpoints(browser_flow, endpoint):
    client, flow, saved, scenario, requests = browser_flow
    scenario[endpoint] = "https://evil.test/endpoint"
    response = client.post("/v1/hermes/auth/login", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert not flow.pending and not saved


def test_callback_rejects_identity_from_wrong_provider(browser_flow):
    client, flow, saved, scenario, requests = browser_flow
    state = begin(client, scenario)
    scenario["identity_provider"] = "wrong-provider"
    response = client.get("/hermes-auth/callback", params={"state": state, "code": "one-use-secret"}, follow_redirects=False)
    assert response.status_code == 400
    assert not saved


@pytest.mark.parametrize("origin", [None, "https://attacker.test"])
def test_login_rejects_cross_site_start(browser_flow, origin):
    client, flow, saved, scenario, requests = browser_flow
    response = client.post("/v1/hermes/auth/login", headers={"Origin": origin} if origin else {})
    assert response.status_code == 403
    assert not flow.pending
    assert not requests


@pytest.mark.parametrize("failure", ["cookie", "state", "expiry", "nonce", "token", "identity", "refresh", "denial"])
def test_callback_rejects_invalid_authorization_without_saving(browser_flow, failure):
    client, flow, saved, scenario, requests = browser_flow
    state = begin(client, scenario)
    params = {"state": state, "code": "one-use-secret"}
    if failure == "cookie":
        client.cookies.clear()
    elif failure == "state":
        params["state"] = "wrong-state"
    elif failure == "expiry":
        flow.pending[state].expires = 0
    elif failure == "nonce":
        scenario["nonce"] = "wrong-nonce"
    elif failure == "token":
        scenario["token_status"] = 400
    elif failure == "identity":
        scenario["identity_status"] = 401
    elif failure == "refresh":
        scenario["refresh"] = ""
    elif failure == "denial":
        params["error"] = "access_denied"
    response = client.get("/hermes-auth/callback", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert not saved
    assert "one-use-secret" not in response.text
    assert "refresh-secret" not in response.text


def test_status_has_no_credentials(browser_flow):
    client, *_ = browser_flow
    response = client.get("/v1/hermes/auth/status")
    assert response.json() == {"web_login_enabled": True, "ready": False, "login_required": True}
    assert response.headers["cache-control"] == "no-store"


def test_browser_endpoints_require_existing_proxy_auth(monkeypatch):
    from proxy_st import app as app_module
    monkeypatch.setattr(app_module, "WS_TOKEN", "operator-only")
    with TestClient(app_module.app) as client:
        assert client.get("/v1/hermes/auth/status").status_code == 401
        assert client.post("/v1/hermes/auth/login").status_code == 401
        assert client.get("/hermes-auth/callback").status_code != 401


@pytest.mark.asyncio
async def test_proxy_login_start_accepts_existing_bearer(monkeypatch):
    from fastapi.responses import JSONResponse
    from proxy_st import app as app_module
    monkeypatch.setattr(app_module, "WS_TOKEN", "synthetic-proxy-token")
    monkeypatch.setattr(app_module.web_oidc, "settings", SETTINGS)
    async def begin_without_network(request):
        return JSONResponse({"authorization_url": "https://login.test/authorize"})
    monkeypatch.setattr(app_module.web_oidc, "begin", begin_without_network)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url=ORIGIN) as client:
        response = await client.post("/v1/hermes/auth/login", headers={"Origin": ORIGIN,
            "Authorization": "Bearer synthetic-proxy-token"})
    assert response.status_code == 200
    assert "synthetic-proxy-token" not in response.text


def test_settings_reject_insecure_or_arbitrary_callback():
    for callback in ["http://st.test/hermes-auth/callback", "https://st.test/other"]:
        with pytest.raises(DashboardAuthError):
            WebOIDCSettings(issuer=ISSUER, client_id="client", callback_url=callback).validate()


def test_callback_log_redaction():
    from proxy_st.log import redact_sensitive_query
    logged = redact_sensitive_query("GET /hermes-auth/callback?code=secret-code&state=secret-state HTTP/1.1")
    assert "secret-code" not in logged and "secret-state" not in logged
