"""Browser OIDC authorization for the shared Hermes service connection.

Start/status require the proxy's existing bearer credential. The callback is
public but requires a single-use state bound to an HttpOnly browser cookie.
Identity tokens are verified by Hermes itself before any credential is saved.
"""
from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .hermes_auth import DashboardAuthError, dashboard_url_join
from .log import logger

COOKIE = "__Secure-hermes-oidc"
TTL = 600


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class WebOIDCSettings:
    issuer: str = ""
    client_id: str = ""
    callback_url: str = ""
    scopes: str = "openid profile email offline_access"
    client_secret: str = ""
    token_auth_method: str = "none"
    provider: str = "self-hosted"

    @classmethod
    def from_env(cls) -> WebOIDCSettings:
        return cls(**{k: os.getenv("HERMES_DASHBOARD_OIDC_" + k.upper(), default) for k, default in {
            "issuer": "", "client_id": "", "callback_url": "", "scopes": "openid profile email offline_access",
            "client_secret": "", "token_auth_method": "none", "provider": "self-hosted",
        }.items()})

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.callback_url)

    def validate(self) -> None:
        for url in (self.issuer, self.callback_url):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise DashboardAuthError("Browser OIDC requires explicit HTTPS issuer and callback URLs")
        if urlsplit(self.callback_url).path != "/hermes-auth/callback":
            raise DashboardAuthError("Browser OIDC callback path must be /hermes-auth/callback")
        if self.token_auth_method not in {"none", "client_secret_basic", "client_secret_post"}:
            raise DashboardAuthError("Unsupported OIDC token authentication method")
        if self.token_auth_method != "none" and not self.client_secret:
            raise DashboardAuthError("OIDC client secret is required for the configured authentication method")
        if not {"openid", "offline_access"} <= set(self.scopes.split()):
            raise DashboardAuthError("Browser OIDC requires openid and offline_access scopes")


@dataclass
class PendingLogin:
    verifier: str
    nonce: str
    binding: str
    expires: float


class BrowserOIDC:
    def __init__(self, settings: WebOIDCSettings, dashboard_url: str,
                 save: Callable[[dict[str, Any]], Awaitable[None]],
                 *, client_factory: Callable[[], httpx.AsyncClient] | None = None):
        self.settings = settings
        self.dashboard_url = dashboard_url
        self.save = save
        self.pending: dict[str, PendingLogin] = {}
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=15, follow_redirects=False))
        if settings.enabled:
            settings.validate()

    async def discovery(self, client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.get(self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or str(data.get("issuer", "")).rstrip("/") != self.settings.issuer.rstrip("/"):
            raise DashboardAuthError("OIDC discovery issuer mismatch")
        for key in ("authorization_endpoint", "token_endpoint"):
            endpoint = data.get(key)
            if not isinstance(endpoint, str) or _origin(endpoint) != _origin(self.settings.issuer):
                raise DashboardAuthError("OIDC endpoint must use the configured issuer origin")
        return data

    async def begin(self, request: Request) -> JSONResponse:
        # The caller also needs WS_TOKEN at the application middleware. Origin
        # binding prevents a cross-site page from installing its own identity.
        if request.headers.get("origin") != _origin(self.settings.callback_url):
            return JSONResponse({"error": "OIDC login must start from the configured SillyTavern origin"}, status_code=403)
        self.pending = {k: v for k, v in self.pending.items() if v.expires > time.time()}
        if len(self.pending) >= 128:
            return JSONResponse({"error": "Too many pending sign-ins; retry shortly"}, status_code=429)
        async with self.client_factory() as client:
            discovery = await self.discovery(client)
        verifier, state, nonce, binding = (secrets.token_urlsafe(32) for _ in range(4))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.pending[state] = PendingLogin(verifier, nonce, binding, time.time() + TTL)
        url = discovery["authorization_endpoint"] + "?" + urlencode({
            "client_id": self.settings.client_id, "redirect_uri": self.settings.callback_url,
            "response_type": "code", "scope": self.settings.scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
            # OIDC offline_access requests need explicit consent unless already granted.
            "prompt": "consent",
        })
        response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, binding, max_age=TTL, httponly=True, secure=True, samesite="lax", path="/hermes-auth")
        return response

    async def complete(self, request: Request) -> None:
        states, codes = request.query_params.getlist("state"), request.query_params.getlist("code")
        if len(states) != 1:
            raise DashboardAuthError("Invalid sign-in state")
        pending = self.pending.get(states[0])
        cookie = request.cookies.get(COOKIE, "")
        if (pending is None or pending.expires <= time.time() or not cookie.isascii()
                or not hmac.compare_digest(cookie, pending.binding)):
            raise DashboardAuthError("Sign-in expired or browser binding missing; start again from SillyTavern")
        del self.pending[states[0]]  # Single use, including failed or denied authorization.
        if request.query_params.get("error") or len(codes) != 1 or not codes[0]:
            raise DashboardAuthError("Sign-in was cancelled or rejected")
        async with self.client_factory() as client:
            discovery = await self.discovery(client)
            data = {"grant_type": "authorization_code", "code": codes[0], "code_verifier": pending.verifier,
                    "client_id": self.settings.client_id, "redirect_uri": self.settings.callback_url}
            headers: dict[str, str] = {}
            if self.settings.token_auth_method == "client_secret_post":
                data["client_secret"] = self.settings.client_secret
            elif self.settings.token_auth_method == "client_secret_basic":
                from urllib.parse import quote_plus
                credential = quote_plus(self.settings.client_id) + ":" + quote_plus(self.settings.client_secret)
                headers["Authorization"] = "Basic " + base64.b64encode(credential.encode()).decode()
            response = await client.post(discovery["token_endpoint"], data=data, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise DashboardAuthError("Invalid OIDC token response")
            id_token, refresh = payload.get("id_token"), payload.get("refresh_token")
            if not isinstance(id_token, str) or not id_token or not isinstance(refresh, str) or not refresh:
                raise DashboardAuthError("OIDC did not issue renewable credentials; check offline_access and refresh_token grants")
            # Hermes performs the signature, issuer, audience and expiry verification
            # with its own provider config. No unverified token is written to disk.
            identity = await client.get(dashboard_url_join(self.dashboard_url, "/api/auth/me"),
                                        headers={"Authorization": f"Bearer {id_token}"})
            identity.raise_for_status()
            verified = identity.json()
            if not isinstance(verified, dict):
                raise DashboardAuthError("Invalid Hermes identity response")
            if verified.get("provider") != self.settings.provider or not verified.get("user_id"):
                raise DashboardAuthError("Hermes did not accept the expected OIDC identity")
            try:
                segment = id_token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
                nonce = claims.get("nonce", "") if isinstance(claims, dict) else ""
            except (ValueError, IndexError, TypeError):
                raise DashboardAuthError("Invalid OIDC identity token") from None
            if not isinstance(nonce, str) or not nonce.isascii() or not hmac.compare_digest(nonce, pending.nonce):
                raise DashboardAuthError("OIDC nonce mismatch")
            await self.save({"access_token": id_token, "refresh_token": refresh, "provider": self.settings.provider,
                             "expires_at": verified["expires_at"]})


def web_auth_router(flow: BrowserOIDC, status: Callable[[], dict[str, Any]]) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/hermes/auth/status")
    async def auth_status() -> JSONResponse:
        return JSONResponse({"web_login_enabled": flow.settings.enabled, **status()}, headers={"Cache-Control": "no-store"})

    @router.post("/v1/hermes/auth/login")
    async def auth_login(request: Request) -> JSONResponse:
        if not flow.settings.enabled:
            return JSONResponse({"error": "Browser OIDC login is not configured"}, status_code=503)
        try:
            return await flow.begin(request)
        except (httpx.HTTPError, ValueError, DashboardAuthError):
            return JSONResponse({"error": "Hermes sign-in is temporarily unavailable"}, status_code=503)

    @router.get("/hermes-auth/callback", response_model=None)
    async def auth_callback(request: Request) -> HTMLResponse | RedirectResponse:
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if not flow.settings.enabled:
            return HTMLResponse("Hermes browser sign-in is not configured.", status_code=503, headers=headers)
        try:
            await flow.complete(request)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, DashboardAuthError, OSError):
            logger.warning("Hermes OIDC browser authorization failed; no credentials saved")
            response: HTMLResponse | RedirectResponse = HTMLResponse(
                "Connexion Hermes impossible ou expirée. Retourne dans SillyTavern et réessaie."
                ' Si le problème persiste, vérifie la configuration OIDC du serveur. <a href="/">Retourner à SillyTavern</a>', status_code=400, headers=headers)
        else:
            logger.info("Hermes OIDC browser authorization succeeded")
            # Fixed return origin: never accept a browser-provided redirect target.
            response = RedirectResponse(_origin(flow.settings.callback_url) + "/", status_code=303, headers=headers)
        response.delete_cookie(COOKIE, path="/hermes-auth", secure=True, httponly=True, samesite="lax")
        return response

    return router
