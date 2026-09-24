from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import WebSocketDisconnect

from proxy_st import app as app_module
from proxy_st import config, relay, voice
from proxy_st.hermes_ws import HermesWebSocketManager
from proxy_st.profile_policy import (
    PROFILE_NOT_ALLOWED_CODE,
    PROFILE_NOT_ALLOWED_MESSAGE,
    ProfileNotAllowedError,
    profile_options_for_client,
    resolve_profile_selection,
    session_profile_override,
)
from proxy_st.request_transform import proxy_profile_override


def test_allowlist_configuration_is_optional_and_extensible() -> None:
    assert config.parse_hermes_profile_allowlist(None) is None
    assert config.parse_hermes_profile_allowlist(" default, local,ONLINE ") == frozenset(
        {"default", "local", "online"}
    )
    with pytest.raises(ValueError, match="invalid profile name"):
        config.parse_hermes_profile_allowlist("default,admin profile")
    with pytest.raises(ValueError, match="must include the default profile"):
        config.parse_hermes_profile_allowlist("")
    with pytest.raises(ValueError, match="must include the default profile"):
        config.parse_hermes_profile_allowlist("local,online")


def test_default_profile_and_private_unrestricted_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    assert resolve_profile_selection("DEFAULT") == "default"
    assert session_profile_override("default") == "default"
    with pytest.raises(ProfileNotAllowedError) as exc_info:
        resolve_profile_selection("online")
    assert str(exc_info.value) == PROFILE_NOT_ALLOWED_MESSAGE

    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", None)
    assert proxy_profile_override({"st_proxy": {"profile": "online"}}) == "online"


def test_profile_metadata_rejects_disallowed_and_malformed_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    assert proxy_profile_override({"st_proxy": {"profile": "default"}}) == "default"
    for profile in ("online", "bad profile", "{{profile}}", 7, {"name": "online"}):
        with pytest.raises(ProfileNotAllowedError):
            proxy_profile_override({"st_proxy": {"profile": profile}})


def test_profile_options_are_filtered_and_default_is_always_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    options = profile_options_for_client({
        "active": "online",
        "profiles": [
            {"name": "online", "description": "private online profile"},
            {"name": "local", "description": "private local profile"},
        ],
        "private_metadata": "must not leak",
    })
    assert options == {"active": "default", "profiles": [{"name": "default"}]}
    assert "online" not in json.dumps(options)
    assert "private" not in json.dumps(options)


@pytest.mark.asyncio
async def test_manager_profile_options_filter_hermes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager = HermesWebSocketManager()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    request = AsyncMock(return_value={
        "active": "online",
        "profiles": [{"name": "online"}, {"name": "local"}],
    })
    monkeypatch.setattr(manager, "_request_json_rpc", request)

    options = await manager.profile_options()

    assert options == {"active": "default", "profiles": [{"name": "default"}]}
    request.assert_awaited_once_with("profiles.list", {}, timeout=10.0)


@pytest.mark.asyncio
async def test_http_profile_options_hide_disallowed_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager = app_module.hermes_ws_manager
    monkeypatch.setattr(manager, "_should_connect", True)
    monkeypatch.setattr(manager, "_ready", True)
    monkeypatch.setattr(manager, "_ws", type("OpenWebSocket", (), {"open": True})())
    lookup = AsyncMock(return_value={
        "active": "online",
        "profiles": [{"name": "default", "path": "/secret/default"}, {"name": "online", "path": "/secret/profile"}],
    })
    monkeypatch.setattr(manager, "_request_json_rpc", lookup)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/hermes/profile_options")

    assert response.status_code == 200
    assert response.json() == {"active": "default", "profiles": [{"name": "default"}]}
    assert "/secret/default" not in response.text
    assert "online" not in response.text
    assert "/secret/profile" not in response.text


@pytest.mark.asyncio
async def test_websocket_profile_options_hide_disallowed_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager = app_module.hermes_ws_manager
    monkeypatch.setattr(manager, "_should_connect", True)
    monkeypatch.setattr(manager, "_ready", True)
    monkeypatch.setattr(manager, "_ws", type("OpenWebSocket", (), {"open": True})())
    lookup = AsyncMock(return_value={
        "active": "online",
        "profiles": [{"name": "default", "path": "/secret/default"}, {"name": "online", "path": "/secret/profile"}],
    })
    monkeypatch.setattr(manager, "_request_json_rpc", lookup)

    class FakeWebSocket:
        headers = {}
        query_params = {}

        def __init__(self) -> None:
            self.messages = iter([{"type": "get_profile_options"}])
            self.sent: list[dict] = []

        async def accept(self, subprotocol: str | None = None) -> None:
            return None

        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

        async def receive_text(self) -> str:
            try:
                return json.dumps(next(self.messages))
            except StopIteration:
                raise WebSocketDisconnect() from None

    websocket = FakeWebSocket()
    await app_module.websocket_endpoint(websocket)

    options = next(item for item in websocket.sent if item.get("type") == "profile_options")
    assert options == {"type": "profile_options", "options": {"active": "default", "profiles": [{"name": "default"}]}}
    assert "/secret/default" not in json.dumps(options)
    assert "/secret/profile" not in json.dumps(options)
    lookup.assert_awaited_once_with("profiles.list", {}, timeout=10.0)


@pytest.mark.asyncio
async def test_http_profile_switch_uses_stable_forbidden_error_without_profile_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    lookup = AsyncMock(return_value={"active": "default", "profiles": [{"name": "default"}]})
    monkeypatch.setattr(app_module.hermes_ws_manager, "_request_json_rpc", lookup)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/session/chat-1/profile", json={"profile": "online"})

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "message": PROFILE_NOT_ALLOWED_MESSAGE,
            "type": "profile_error",
            "code": PROFILE_NOT_ALLOWED_CODE,
        }
    }
    assert "online" not in response.text
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_profile_switch_uses_stable_forbidden_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "WS_TOKEN", "")
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))

    class FakeWebSocket:
        headers = {}
        query_params = {}

        def __init__(self) -> None:
            self.messages = iter([{"type": "set_profile", "session_id": "chat-1", "profile": "admin"}])
            self.sent: list[dict] = []

        async def accept(self, subprotocol: str | None = None) -> None:
            return None

        async def send_json(self, payload: dict) -> None:
            self.sent.append(payload)

        async def receive_text(self) -> str:
            try:
                return json.dumps(next(self.messages))
            except StopIteration:
                raise WebSocketDisconnect() from None

    websocket = FakeWebSocket()
    await app_module.websocket_endpoint(websocket)

    error = websocket.sent[-1]
    assert error == {
        "type": "error",
        "code": PROFILE_NOT_ALLOWED_CODE,
        "message": PROFILE_NOT_ALLOWED_MESSAGE,
    }
    assert "admin" not in json.dumps(error)


@pytest.mark.asyncio
async def test_chat_metadata_is_rejected_before_prompt_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))

    class FakeManager:
        is_connected = True
        submit_prompt = AsyncMock()

        def has_prompt_history(self, _session_id: str) -> bool:
            return False

    manager = FakeManager()
    response = await relay.forward_hermes_streaming(
        {
            "messages": [{"role": "user", "content": "do not forward this prompt"}],
            "st_proxy": {"session_id": "chat-1", "profile": "online"},
        },
        "chat-1",
        manager,
    )

    assert response.status_code == 403
    assert json.loads(response.body) == {
        "error": {
            "message": PROFILE_NOT_ALLOWED_MESSAGE,
            "type": "profile_error",
            "code": PROFILE_NOT_ALLOWED_CODE,
        }
    }
    manager.submit_prompt.assert_not_awaited()
    assert "online" not in response.body.decode()


@pytest.mark.asyncio
async def test_voice_start_rejects_disallowed_profile_before_token_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    monkeypatch.setattr(voice, "WS_TOKEN", "browser-secret")
    voice.calls.clear()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/voice/calls",
            headers={"Authorization": "Bearer browser-secret"},
            json={"session_id": "voice-chat", "profile": "online"},
        )

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "message": PROFILE_NOT_ALLOWED_MESSAGE,
            "type": "profile_error",
            "code": PROFILE_NOT_ALLOWED_CODE,
        }
    }
    assert "online" not in response.text
    assert voice.calls == {}
