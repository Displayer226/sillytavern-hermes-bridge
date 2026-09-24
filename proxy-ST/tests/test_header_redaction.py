"""Focused tests for x-proxy-token log redaction.

The proxy authenticates requests via the x-proxy-token header
(proxy_st/auth.py), so that value can appear on inbound requests and
must never reach requests.jsonl or the debug header logs.
sanitize_headers() masks every sensitive header with the repository's
expected mask, case-insensitively (keys are lowercased before the set
membership check).
"""
from __future__ import annotations

import json

import httpx
import pytest

from proxy_st import app as app_module
from proxy_st import logging_utils
from proxy_st.config import SENSITIVE_HEADERS
from proxy_st.utils import sanitize_headers


def test_x_proxy_token_is_listed_as_sensitive_header():
    assert "x-proxy-token" in SENSITIVE_HEADERS


def test_sanitize_headers_masks_x_proxy_token_case_insensitively():
    sanitized = sanitize_headers(
        {
            "X-Proxy-Token": "upper-secret",
            "x-PROXY-tOkEn": "mixed-secret",
            "x-proxy-token": "lower-secret",
            "Authorization": "Bearer also-secret",
            "x-st-session": "chat-1",
        }
    )

    assert sanitized["x-proxy-token"] == "***redacted***"
    assert sanitized["authorization"] == "***redacted***"
    # Every casing variant collapses onto the lowercased key and is masked.
    assert "upper-secret" not in sanitized.values()
    assert "mixed-secret" not in sanitized.values()
    assert "lower-secret" not in sanitized.values()
    # Non-sensitive headers keep their value.
    assert sanitized["x-st-session"] == "chat-1"


@pytest.mark.asyncio
async def test_requests_jsonl_masks_x_proxy_token(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(logging_utils, "LOG_DIR", tmp_path)
    monkeypatch.setattr(logging_utils, "_request_jsonl_logger", None)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/no-such-jsonl-route",
            headers={"X-Proxy-Token": "topsecret-token-value"},
        )

    # The catch-all route logs the request even though it answers 404.
    assert response.status_code == 404

    log_file = tmp_path / "requests.jsonl"
    assert log_file.exists(), "requests.jsonl was not written"
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matching = [entry for entry in entries if entry["path"] == "/no-such-jsonl-route"]
    assert matching, "request was not logged to requests.jsonl"

    entry = matching[-1]
    assert entry["headers"]["x-proxy-token"] == "***redacted***"
    assert "topsecret-token-value" not in json.dumps(entries, ensure_ascii=False)
