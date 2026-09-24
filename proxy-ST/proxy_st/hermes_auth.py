from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import httpx

logger = logging.getLogger("sillytavern-session-proxy.hermes_auth")


class DashboardAuthError(RuntimeError):
    """Raised when the Hermes dashboard auth flow cannot mint a WS ticket."""


def dashboard_url_join(base_url: str, path: str) -> str:
    """Join a Hermes dashboard base URL with an auth path, preserving prefixes."""
    base = (base_url or "").strip()
    if not base:
        return path
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _http_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return f"{exc.__class__.__name__}: {text}"
    return exc.__class__.__name__


def _response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except json.JSONDecodeError:
        text = response.text.strip()
        return text[:200] if text else response.reason_phrase
    if isinstance(data, dict):
        detail = data.get("detail") or data.get("error") or data.get("message")
        if detail:
            return str(detail)[:200]
    return response.reason_phrase


@dataclass(frozen=True)
class HermesDashboardPasswordAuth:
    provider: str = "basic"
    username: str = ""
    password: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)


class HermesDashboardAuthenticator:
    """Password-login client for the authenticated Hermes dashboard."""

    def __init__(
        self,
        dashboard_url: str,
        *,
        auth: HermesDashboardPasswordAuth | None = None,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.dashboard_url = dashboard_url
        self.auth = auth or HermesDashboardPasswordAuth()
        self.timeout = timeout
        self._client = client
        self._client_factory = client_factory
        self._owns_client = client is None

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
            self._owns_client = True
        return self._client

    async def fetch_ws_ticket(self) -> str:
        """Return a fresh single-use WebSocket ticket for the current session."""
        response = await self._mint_ticket()
        if response.status_code in {401, 403}:
            await self._login()
            response = await self._mint_ticket()

        if response.status_code != 200:
            raise DashboardAuthError(
                f"Hermes dashboard ws-ticket failed: HTTP {response.status_code} {_response_detail(response)}"
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise DashboardAuthError("Hermes dashboard ws-ticket returned invalid JSON") from exc

        ticket = payload.get("ticket") if isinstance(payload, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise DashboardAuthError("Hermes dashboard ws-ticket response did not contain a ticket")

        logger.info("Hermes dashboard WebSocket ticket minted")
        return ticket

    async def _mint_ticket(self) -> httpx.Response:
        client = self._get_client()
        url = dashboard_url_join(self.dashboard_url, "/api/auth/ws-ticket")
        try:
            return await client.post(url)
        except httpx.HTTPError as exc:
            raise DashboardAuthError(f"Hermes dashboard ws-ticket request failed: {_http_error_message(exc)}") from exc

    async def _login(self) -> None:
        if not self.auth.is_configured:
            raise DashboardAuthError(
                "Hermes dashboard password auth requires HERMES_DASHBOARD_AUTH_USERNAME "
                "and HERMES_DASHBOARD_AUTH_PASSWORD"
            )

        client = self._get_client()
        url = dashboard_url_join(self.dashboard_url, "/auth/password-login")
        payload = {
            "provider": self.auth.provider,
            "username": self.auth.username,
            "password": self.auth.password,
            "next": "/",
        }
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise DashboardAuthError(f"Hermes dashboard login request failed: {_http_error_message(exc)}") from exc

        if response.status_code != 200:
            raise DashboardAuthError(
                f"Hermes dashboard login failed: HTTP {response.status_code} {_response_detail(response)}"
            )
        logger.info("Hermes dashboard password login succeeded with provider=%s", self.auth.provider)
