"""Hermes native OIDC login and persistent refresh-token authentication.

Run ``python -m proxy_st.hermes_oidc --help`` for the one-time browser login.
Token files belong in private runtime storage, never in the source checkout.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from .hermes_auth import DashboardAuthError, dashboard_url_join, response_retry_after


class OIDCLoginRequired(DashboardAuthError):
    """The operator must run the browser login command again."""


LOGIN_REQUIRED = "Hermes OIDC login required: run python -m proxy_st.hermes_oidc to connect or renew the saved session"


def _validate_tokens(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or any(not isinstance(data.get(k), str) or not data[k] for k in (
        "access_token", "refresh_token", "provider",
    )):
        raise DashboardAuthError("Invalid Hermes OIDC token response; no credentials saved")
    expiry = data.get("expires_at")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
        raise DashboardAuthError("Invalid Hermes OIDC token expiry; no credentials saved")
    return {k: data[k] for k in ("access_token", "refresh_token", "provider", "expires_at")}


def save_tokens(path: Path, data: dict[str, Any], dashboard_url: str, *, login_required: bool = False) -> None:
    """Atomic 0600 replacement also persists rotated refresh tokens."""
    payload = _validate_tokens(data)
    payload["dashboard_url"] = dashboard_url.rstrip("/")
    if login_required:
        payload["login_required"] = True
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".hermes-oidc-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_tokens(path: Path, dashboard_url: str) -> dict[str, Any]:
    try:
        if path.stat().st_mode & 0o077:
            raise DashboardAuthError("Hermes OIDC token file must have mode 0600")
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise OIDCLoginRequired(LOGIN_REQUIRED) from None
    except (OSError, ValueError):
        raise DashboardAuthError("Cannot read Hermes OIDC token file") from None
    tokens = _validate_tokens(data)
    if data.get("dashboard_url") != dashboard_url.rstrip("/"):
        raise DashboardAuthError("Hermes OIDC token file belongs to a different dashboard URL; sign in again")
    if data.get("login_required"):
        raise OIDCLoginRequired(LOGIN_REQUIRED)
    return tokens


async def _refresh(client: httpx.AsyncClient, dashboard_url: str, path: Path, tokens: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(dashboard_url_join(dashboard_url, "/auth/native/refresh"), json={
        "refresh_token": tokens["refresh_token"], "provider": tokens["provider"],
    })
    # The pinned Hermes native refresh contract uses 401 for a rejected token;
    # other statuses can also come from an intermediary or a configuration error.
    if response.status_code == 401:
        save_tokens(path, tokens, dashboard_url, login_required=True)
        raise OIDCLoginRequired(LOGIN_REQUIRED)
    if response.status_code != 200:
        raise DashboardAuthError(f"Hermes OIDC refresh unavailable: HTTP {response.status_code}; saved credentials retained",
                                 retry_after=response_retry_after(response))
    try:
        updated = _validate_tokens(response.json())
    except ValueError:
        raise DashboardAuthError("Invalid Hermes OIDC refresh response") from None
    save_tokens(path, updated, dashboard_url)
    return updated


async def fetch_oidc_ticket(client: httpx.AsyncClient, dashboard_url: str, token_file: str) -> str:
    """Called under the authenticator lock; only one refresh runs at a time."""
    if not token_file:
        raise OIDCLoginRequired(LOGIN_REQUIRED)
    path = Path(token_file)
    tokens = load_tokens(path, dashboard_url)
    refreshed = False
    try:
        if tokens["expires_at"] <= time.time() + 30:
            tokens = await _refresh(client, dashboard_url, path, tokens)
            refreshed = True
        response = await client.post(dashboard_url_join(dashboard_url, "/api/auth/ws-ticket"),
                                     headers={"Authorization": f"Bearer {tokens['access_token']}"})
        if response.status_code in {401, 403} and not refreshed:
            tokens = await _refresh(client, dashboard_url, path, tokens)
            response = await client.post(dashboard_url_join(dashboard_url, "/api/auth/ws-ticket"),
                                         headers={"Authorization": f"Bearer {tokens['access_token']}"})
    except httpx.HTTPError:
        raise DashboardAuthError("Hermes OIDC request unavailable; saved credentials retained") from None
    if response.status_code in {401, 403}:
        # Ticket authorization can fail even when refresh succeeded. Only a
        # definitive refresh rejection proves that renewed login is required.
        raise DashboardAuthError(f"Hermes OIDC ws-ticket rejected: HTTP {response.status_code}",
                                 retry_after=response_retry_after(response))
    if response.status_code != 200:
        raise DashboardAuthError(f"Hermes OIDC ws-ticket failed: HTTP {response.status_code}",
                                 retry_after=response_retry_after(response))
    try:
        data = response.json()
    except ValueError:
        raise DashboardAuthError("Invalid Hermes OIDC ws-ticket response") from None
    ticket = data.get("ticket") if isinstance(data, dict) else None
    if not isinstance(ticket, str) or not ticket:
        raise DashboardAuthError("Hermes OIDC ws-ticket response did not contain a ticket")
    return ticket


def callback_code(callback_url: str, redirect_uri: str, state: str) -> str:
    target, expected = urlsplit(callback_url), urlsplit(redirect_uri)
    query = parse_qs(target.query)
    if (target.scheme, target.netloc, target.path) != (expected.scheme, expected.netloc, expected.path):
        raise DashboardAuthError("Unexpected OIDC callback address")
    if not hmac.compare_digest(query.get("state", [""])[0], state):
        raise DashboardAuthError("OIDC callback state mismatch")
    if query.get("error") or len(query.get("code", [])) != 1 or len(query.get("state", [])) != 1:
        raise DashboardAuthError("OIDC authorization failed or callback is invalid")
    return query["code"][0]


def login(args: argparse.Namespace) -> None:
    """Public authorize + exchange use the same Hermes dashboard process."""
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    received: list[str] = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass  # Callback URLs carry single-use authorization codes.

        def do_GET(self) -> None:
            try:
                code = callback_code(f"http://127.0.0.1:{server.server_port}{self.path}", redirect_uri, state)
            except DashboardAuthError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid callback. Return to the login command.")
                return
            received.append(code)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Hermes authorization received. You can close this tab.")

    server = HTTPServer(("127.0.0.1", args.port), Callback)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    try:
        query = urlencode({"provider": args.provider, "code_challenge": challenge, "code_challenge_method": "S256",
                           "redirect_uri": redirect_uri, "state": state})
        url = dashboard_url_join(args.public_url, "/auth/native/authorize") + "?" + query
        print("Open this URL in your browser:\n" + url, flush=True)
        if args.manual:
            print("After login, the browser may show a connection error. Copy the FULL final callback URL here.")
            received.append(callback_code(input("Callback URL: ").strip(), redirect_uri, state))
        else:
            webbrowser.open(url)
            deadline = time.monotonic() + 300
            server.timeout = 1
            while not received and time.monotonic() < deadline:
                server.handle_request()
            if not received:
                raise DashboardAuthError("OIDC login timed out; run the command again")
        # Do not send the exchange to a different serve process: its authorization store is in memory.
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(dashboard_url_join(args.exchange_url or args.public_url, "/auth/native/token"),
                                   json={"code": received[0], "code_verifier": verifier})
        if response.status_code != 200:
            raise DashboardAuthError(f"Hermes OIDC code exchange failed: HTTP {response.status_code}")
        try:
            tokens = _validate_tokens(response.json())
        except ValueError:
            raise DashboardAuthError("Invalid Hermes OIDC code exchange response") from None
        save_tokens(Path(args.token_file), tokens, args.dashboard_url)
        print("Hermes OIDC session saved. The proxy can now connect.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect the proxy to Hermes using a browser OIDC login (PKCE).")
    parser.add_argument("--public-url", required=True, help="Public Hermes dashboard URL (same identity provider as the runtime serve instance)")
    parser.add_argument("--exchange-url", default="", help="Internal URL of the PUBLIC dashboard process for code exchange; bypasses outer reverse-proxy login")
    parser.add_argument("--dashboard-url", default=os.getenv("HERMES_DASHBOARD_URL", "http://host.docker.internal:9119/"),
                        help="Runtime dashboard URL used by the container (bound into the saved credential)")
    parser.add_argument("--token-file", default=os.getenv("HERMES_DASHBOARD_OIDC_TOKEN_FILE", "/app/data/hermes-oidc.json"))
    parser.add_argument("--provider", default="self-hosted")
    parser.add_argument("--port", type=int, default=0, help="Loopback callback port; 0 selects a free port")
    parser.add_argument("--manual", action="store_true", help="Paste the final callback URL when the browser is on another machine")
    args = parser.parse_args()
    for value in (args.public_url, args.dashboard_url, args.exchange_url or args.public_url):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            parser.error("Dashboard URLs must be HTTP(S) base URLs without credentials, query, or fragment")
    try:
        login(args)
    except (DashboardAuthError, OSError, ValueError, httpx.HTTPError, EOFError, KeyboardInterrupt) as exc:
        # Never print response bodies, authorization codes, access tokens or refresh tokens.
        message = str(exc) if isinstance(exc, DashboardAuthError) else type(exc).__name__
        parser.exit(1, f"Login failed: {message}\n")


if __name__ == "__main__":
    main()
