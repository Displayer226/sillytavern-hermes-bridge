from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from typing import Any, Callable

import httpx

logger = logging.getLogger("sillytavern-session-proxy.hermes_auth")


class DashboardAuthError(RuntimeError):
    """Raised when the Hermes dashboard auth flow cannot mint a WS ticket."""

    def __init__(self, message: str, *, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def response_retry_after(response: httpx.Response) -> float:
    """Accept both HTTP Retry-After forms without trusting malformed values."""
    value = response.headers.get("Retry-After", "")
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0



def dashboard_url_join(base_url: str, path: str) -> str:
    """Join a Hermes dashboard base URL with an auth path, preserving prefixes."""
    base = (base_url or "").strip()
    if not base:
        return path
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


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
        oidc_token_file: str = "",
        client: httpx.AsyncClient | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.dashboard_url = dashboard_url
        self.auth = auth or HermesDashboardPasswordAuth()
        self.timeout = timeout
        self.oidc_token_file = oidc_token_file
        self._ticket_lock = asyncio.Lock()
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
        async with self._ticket_lock:
            return await self._fetch_ws_ticket()

    async def store_oidc_session(self, tokens: dict[str, Any], token_file: str) -> None:
        from pathlib import Path
        from .hermes_oidc import save_tokens

        async with self._ticket_lock:
            save_tokens(Path(token_file), tokens, self.dashboard_url)

    async def _fetch_ws_ticket(self) -> str:
        if self.oidc_token_file:
            from .hermes_oidc import fetch_oidc_ticket

            return await fetch_oidc_ticket(self._get_client(), self.dashboard_url, self.oidc_token_file)
        response = await self._mint_ticket()
        if response.status_code in {401, 403}:
            await self._login()
            response = await self._mint_ticket()

        if response.status_code != 200:
            raise DashboardAuthError(
                f"Hermes dashboard ws-ticket failed: HTTP {response.status_code}",
                retry_after=response_retry_after(response),
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
        except httpx.HTTPError:
            raise DashboardAuthError("Hermes dashboard ws-ticket request unavailable") from None

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
        except httpx.HTTPError:
            raise DashboardAuthError("Hermes dashboard login request unavailable") from None

        if response.status_code != 200:
            raise DashboardAuthError(
                f"Hermes dashboard login failed: HTTP {response.status_code}",
                retry_after=response_retry_after(response),
            )
        logger.info("Hermes dashboard password login succeeded with provider=%s", self.auth.provider)
