"""Focused tests for WebSocket subprotocol authentication and log redaction.

The /ws endpoint authenticates through the WebSocket subprotocol list:
  ["sillytavern-hermes-bridge", "auth.<base64url(UTF-8(token))>"]
(base64url without padding). The legacy `?token=` query parameter is only
accepted while PROXY_WS_QUERY_TOKEN_COMPAT is enabled. Rejected handshake
requests must never leak the token into logs, and subprotocol header values
are never logged.
"""
from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from proxy_st import app as app_module
from proxy_st.config import (
    SENSITIVE_QUERY_PARAMS,
    WS_APPLICATION_SUBPROTOCOL,
    WS_AUTH_SUBPROTOCOL_PREFIX,
)
from proxy_st.log import SensitiveQueryRedactionFilter, install_sensitive_query_redaction


def auth_subprotocol(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")
    return WS_AUTH_SUBPROTOCOL_PREFIX + encoded.rstrip("=")


class FakeWebSocket:
    """Minimal WebSocket double driving websocket_endpoint directly."""

    def __init__(self, subprotocols: list[str] | None = None, query: str = ""):
        self.scope = {"subprotocols": subprotocols or []}
        self.query_params = {"token": query} if query else {}
        self.headers = {}
        self.accepted_with: list[str] | None = None
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict] = []

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted_with = subprotocol

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        raise WebSocketDisconnect() from None


def _ws_app_module(monkeypatch, token: str, compat: bool = False):
    monkeypatch.setattr(app_module, "WS_TOKEN", token)
    monkeypatch.setattr(app_module, "WS_QUERY_TOKEN_COMPAT", compat)
    return app_module


# --- Valid authentication ---------------------------------------------


@pytest.mark.asyncio
async def test_valid_subprotocols_are_accepted_and_app_protocol_selected(monkeypatch):
    _ws_app_module(monkeypatch, "secret-token")

    ws = FakeWebSocket(subprotocols=[WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("secret-token")])
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None
    assert ws.accepted_with == WS_APPLICATION_SUBPROTOCOL
    assert ws.sent and ws.sent[0]["type"] == "connected"


@pytest.mark.asyncio
async def test_token_encodings_for_base64url_values_round_trip(monkeypatch):
    """A token whose base64url form needs padding must still authenticate."""
    token = " secret/+/=tokens with spaces "  # bytes outside the base64url alphabet
    _ws_app_module(monkeypatch, token)

    ws = FakeWebSocket(subprotocols=[WS_APPLICATION_SUBPROTOCOL, auth_subprotocol(token)])
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None
    assert ws.accepted_with == WS_APPLICATION_SUBPROTOCOL


# --- Invalid authentication -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subprotocols",
    [
        [],  # missing application protocol and missing auth
        [WS_APPLICATION_SUBPROTOCOL],  # missing auth subprotocol
        [auth_subprotocol("secret-token")],  # missing application protocol
        [WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("wrong-secret")],  # wrong secret
        [WS_APPLICATION_SUBPROTOCOL, "auth.!!!not-base64url!!!"],  # malformed base64url
        [WS_APPLICATION_SUBPROTOCOL, "auth."],  # empty auth payload
        [WS_APPLICATION_SUBPROTOCOL, "legacy-plain-token"],  # non-auth extra protocol
        [WS_APPLICATION_SUBPROTOCOL, WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("secret-token")],  # duplicate app
        [WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("secret-token"), auth_subprotocol("secret-token")],  # duplicate auth
        [WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("secret-token"), "somethingelse.a"],  # extra invalid protocol
    ],
)
async def test_invalid_subprotocol_combinations_are_rejected(monkeypatch, subprotocols):
    _ws_app_module(monkeypatch, "secret-token")

    ws = FakeWebSocket(subprotocols=subprotocols)
    await app_module.websocket_endpoint(ws)

    assert ws.closed == (4001, "Unauthorized")
    assert ws.accepted_with is None


@pytest.mark.asyncio
async def test_padded_base64url_is_rejected(monkeypatch):
    """Padding is not part of the client convention; it must not validate."""
    _ws_app_module(monkeypatch, "x")
    padded = WS_AUTH_SUBPROTOCOL_PREFIX + base64.urlsafe_b64encode(b"x").decode("ascii")

    ws = FakeWebSocket(subprotocols=[WS_APPLICATION_SUBPROTOCOL, padded])
    await app_module.websocket_endpoint(ws)

    assert ws.closed == (4001, "Unauthorized")


@pytest.mark.asyncio
async def test_rejection_warning_never_contains_token(monkeypatch, caplog):
    # Handshake presents a secret-looking auth subprotocol that does NOT
    # match the configured token, so the request is rejected; the warning
    # must never echo the token or any subprotocol value.
    _ws_app_module(monkeypatch, "expected-server-secret")

    ws = FakeWebSocket(subprotocols=[WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("super-secret-token-value")])
    with caplog.at_level(logging.WARNING, logger="sillytavern-session-proxy"):
        await app_module.websocket_endpoint(ws)

    assert ws.closed == (4001, "Unauthorized")
    assert "super-secret-token-value" not in caplog.text
    assert "auth." not in caplog.text


# --- Legacy ?token= behavior in both flag modes ----------------------


@pytest.mark.asyncio
async def test_legacy_query_token_rejected_when_compat_disabled(monkeypatch):
    _ws_app_module(monkeypatch, "secret-token", compat=False)

    ws = FakeWebSocket(query="secret-token")
    await app_module.websocket_endpoint(ws)

    assert ws.closed == (4001, "Unauthorized")


@pytest.mark.asyncio
async def test_legacy_query_token_rejected_when_wrong_even_with_compat(monkeypatch):
    _ws_app_module(monkeypatch, "secret-token", compat=True)

    ws = FakeWebSocket(query="wrong-token")
    await app_module.websocket_endpoint(ws)

    assert ws.closed == (4001, "Unauthorized")


@pytest.mark.asyncio
async def test_legacy_query_token_accepted_temporarily_with_compat(monkeypatch):
    _ws_app_module(monkeypatch, "secret-token", compat=True)

    ws = FakeWebSocket(query="secret-token")
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None
    assert ws.accepted_with is None


@pytest.mark.asyncio
async def test_subprotocol_auth_also_works_with_compat_enabled(monkeypatch):
    _ws_app_module(monkeypatch, "secret-token", compat=True)

    ws = FakeWebSocket(subprotocols=[WS_APPLICATION_SUBPROTOCOL, auth_subprotocol("secret-token")])
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None


@pytest.mark.asyncio
async def test_no_token_configured_keeps_development_open_behavior(monkeypatch):
    _ws_app_module(monkeypatch, "", compat=False)

    ws = FakeWebSocket()
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None
    assert ws.accepted_with is None


@pytest.mark.asyncio
async def test_no_token_selects_only_an_exactly_offered_application_protocol(monkeypatch):
    _ws_app_module(monkeypatch, "", compat=False)

    ws = FakeWebSocket(subprotocols=[WS_APPLICATION_SUBPROTOCOL])
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None
    assert ws.accepted_with == WS_APPLICATION_SUBPROTOCOL


@pytest.mark.asyncio
async def test_no_token_never_selects_an_unoffered_protocol(monkeypatch):
    _ws_app_module(monkeypatch, "", compat=False)

    ws = FakeWebSocket(subprotocols=["some-other-protocol"])
    await app_module.websocket_endpoint(ws)

    assert ws.closed is None
    assert ws.accepted_with is None


def test_real_asgi_handshake_negotiates_modern_and_legacy_clients(monkeypatch):
    """Exercise Starlette's actual WebSocket handshake path with fake secrets."""
    token = "synthetic-handshake-secret"
    _ws_app_module(monkeypatch, token, compat=True)

    async def no_op_start():
        return None

    async def no_op_stop():
        return None

    monkeypatch.setattr(app_module.hermes_ws_manager, "start", no_op_start)
    monkeypatch.setattr(app_module.hermes_ws_manager, "stop", no_op_stop)

    with TestClient(app_module.app) as client:
        with client.websocket_connect(
            "/ws",
            subprotocols=[WS_APPLICATION_SUBPROTOCOL, auth_subprotocol(token)],
        ) as websocket:
            assert websocket.accepted_subprotocol == WS_APPLICATION_SUBPROTOCOL
            assert websocket.receive_json()["type"] == "connected"

        with client.websocket_connect("/ws?token=synthetic-handshake-secret") as websocket:
            assert websocket.accepted_subprotocol is None
            assert websocket.receive_json()["type"] == "connected"


# --- Uvicorn-style access-log redaction -------------------------------


def _uvicorn_access_record(line: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=line,
        args=(),
        exc_info=None,
    )


def test_uvicorn_formatted_access_log_redacts_rejected_websocket_query():
    log_filter = SensitiveQueryRedactionFilter()
    line = '127.0.0.1:52114 - "GET /ws?token=leaky-token-value HTTP/1.1" 401'
    record = _uvicorn_access_record(line)

    assert log_filter.filter(record) is True
    assert "leaky-token-value" not in record.getMessage()
    assert "/ws?token=***redacted***" in record.getMessage()


def test_redaction_covers_all_configured_sensitive_params():
    log_filter = SensitiveQueryRedactionFilter()
    line = "GET /x?token=a&access_token=b&api_key=c&key=d HTTP/1.1"
    record = _uvicorn_access_record(line)
    assert log_filter.filter(record) is True
    rendered = record.getMessage()
    for value in ("a", "b", "c", "d"):
        assert f"={value}" not in rendered
    assert "token=***redacted***" in rendered
    assert "access_token=***redacted***" in rendered
    assert "api_key=***redacted***" in rendered
    assert "key=***redacted***" in rendered


def test_redaction_is_idempotent():
    log_filter = SensitiveQueryRedactionFilter()
    line = "GET /ws?token=secret-value HTTP/1.1"

    first = _uvicorn_access_record(line)
    assert log_filter.filter(first) is True
    once = first.getMessage()
    assert log_filter.filter(first) is True
    assert first.getMessage() == once

    redacted_line = "GET /ws?token=***redacted*** HTTP/1.1"
    record = _uvicorn_access_record(redacted_line)
    assert log_filter.filter(record) is True
    assert record.getMessage() == redacted_line


def test_redaction_preserves_deferred_log_formatting():
    log_filter = SensitiveQueryRedactionFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='GET %s %s',
        args=("/ws?token=deferred-secret", "HTTP/1.1"),
        exc_info=None,
    )

    assert log_filter.filter(record) is True
    assert record.getMessage() == "GET /ws?token=***redacted*** HTTP/1.1"
    assert "deferred-secret" not in record.getMessage()


def test_install_sensitive_query_redaction_is_idempotent():
    logger = logging.getLogger("uvicorn.access")
    handler = logging.NullHandler()
    logger.handlers = [handler]
    try:
        install_sensitive_query_redaction()
        count = len(handler.filters)
        assert count == 1
        assert isinstance(handler.filters[0], SensitiveQueryRedactionFilter)

        install_sensitive_query_redaction()
        assert len(handler.filters) == count
    finally:
        logger.handlers = []


def test_root_handlers_receive_redaction_filter_on_import():
    installed = any(
        isinstance(f, SensitiveQueryRedactionFilter)
        for handler in logging.getLogger().handlers
        for f in handler.filters
    )
    assert installed, "proxy_st.log import must install the redaction filter"


def test_sensitive_query_params_include_token():
    assert "token" in SENSITIVE_QUERY_PARAMS


# --- Existing HTTP auth regression (unchanged semantics) --------------


@pytest.mark.asyncio
async def test_http_bearer_auth_still_required_for_api_routes(monkeypatch):
    monkeypatch.setattr(app_module, "WS_TOKEN", "secret")
    monkeypatch.setattr(app_module, "WS_QUERY_TOKEN_COMPAT", False)
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
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_websocket_query_redaction_in_jsonl_request_logs(tmp_path, monkeypatch):
    """Rejected handshake attempts logged by request paths stay redacted."""
    from starlette.requests import Request

    from proxy_st import logging_utils
    from proxy_st.logging_utils import log_request

    monkeypatch.setattr(logging_utils, "LOG_DIR", tmp_path)
    monkeypatch.setattr(logging_utils, "_request_jsonl_logger", None)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": b"token=leaky-query-token",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "state": {},
    }
    request = Request(scope)

    async def receive():
        return {"type": "http.disconnect"}

    request._receive = receive
    entry = await log_request(request, {}, "")
    # sanitize_query re-encodes the query string (urlencode quotes the mask),
    # so assert on behavior rather than the exact rendered form.
    assert "leaky-query-token" not in entry["query"]
    assert entry["query"].startswith("token=")
    assert "redacted" in entry["query"]

    log_file = tmp_path / "requests.jsonl"
    assert log_file.exists()
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries
    assert "leaky-query-token" not in json.dumps(entries, ensure_ascii=False)
