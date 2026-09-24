from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from proxy_st import config
import proxy_st.hermes_ws as hermes_ws
from proxy_st.hermes_ws import HermesSession, HermesWebSocketManager


class FakeHermesSocket:
    open = True

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _manager_with_session() -> tuple[HermesWebSocketManager, HermesSession]:
    manager = HermesWebSocketManager()
    session = HermesSession(tui_session_id="tui-1", st_session_id="chat-1")
    manager._sessions[session.tui_session_id] = session
    manager._st_to_tui[session.st_session_id] = session.tui_session_id
    manager._should_connect = True
    manager._ready = True
    manager._ws = FakeHermesSocket()
    return manager, session


def _approval(rpc_id: str, tui_id: str = "tui-1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "approval",
        "params": {
            "session_id": tui_id,
            "request_id": f"approval-{rpc_id}",
            "command": "echo redacted",
            "description": "Approval required",
            "choices": ["once", "session", "always", "deny"],
        },
    }


def test_native_request_is_not_classified_as_proxy_response_and_keeps_rpc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    browser = object()
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(hermes_ws, "ws_broadcast", fake_broadcast)
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda ws, sid: ws is browser and sid == "chat-1")

    asyncio.run(manager._handle_message(json.dumps(_approval("srq-aaa111"))))

    assert list(manager._server_requests) == ["srq-aaa111"]
    assert broadcasts[0] == {
        "type": "server_request",
        "session_id": "chat-1",
        "rpc_id": "srq-aaa111",
        "method": "approval",
        "params": {
            "request_id": "approval-srq-aaa111",
            "command": "echo redacted",
            "description": "Approval required",
            "choices": ["once", "session", "always", "deny"],
        },
    }

    asyncio.run(manager.respond_server_request(
        browser, "chat-1", "srq-aaa111", "approval", {"choice": "once", "all": False},
    ))
    assert manager._ws.sent[-1] == {
        "jsonrpc": "2.0",
        "id": "srq-aaa111",
        "result": {"choice": "once", "all": False},
    }
    with pytest.raises(ValueError):
        asyncio.run(manager.respond_server_request(
            browser, "chat-1", "srq-aaa111", "approval", {"choice": "deny"},
        ))


@pytest.mark.parametrize("choice", ["once", "session", "always", "deny"])
def test_approval_result_choices_are_allowlisted(choice: str, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    browser = object()
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    asyncio.run(manager._handle_server_request(_approval("srq-abc123")))

    asyncio.run(manager.respond_server_request(
        browser, "chat-1", "srq-abc123", "approval", {"choice": choice},
    ))
    assert manager._ws.sent[-1]["id"] == "srq-abc123"


def test_foreign_or_unsubscribed_browser_cannot_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    browser = object()
    monkeypatch.setattr(hermes_ws, "ws_broadcast", _noop_broadcast)
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: False)
    asyncio.run(manager._handle_server_request(_approval("srq-foreign1")))

    with pytest.raises(ValueError):
        asyncio.run(manager.respond_server_request(
            browser, "chat-2", "srq-foreign1", "approval", {"choice": "once"},
        ))
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    with pytest.raises(ValueError):
        asyncio.run(manager.respond_server_request(
            browser, "chat-2", "srq-foreign1", "approval", {"choice": "once"},
        ))
    assert manager._server_requests["srq-foreign1"].st_session_id == "chat-1"


def test_unsupported_method_is_rejected_without_echoing_params(caplog: pytest.LogCaptureFixture) -> None:
    manager, _session = _manager_with_session()
    asyncio.run(manager._handle_message(json.dumps({
        "jsonrpc": "2.0",
        "id": "srq-unsupported",
        "method": "secret",
        "params": {"session_id": "tui-1", "value": "private-secret"},
    })))

    assert manager._ws.sent == [{
        "jsonrpc": "2.0",
        "id": "srq-unsupported",
        "error": {"code": -32601, "message": "Method not supported"},
    }]
    assert "private-secret" not in caplog.text
    assert "secret" not in json.dumps(manager._server_requests)


def test_simple_clarify_batch_lock_and_sudo_are_native(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    browser = object()
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    monkeypatch.setattr(hermes_ws, "ws_broadcast", _noop_broadcast)

    async def fake_request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        assert method == "clarify.lock"
        assert params["request_id"] == "srq-batch01"
        return {"remaining": ["q2"] if params["question_id"] == "q1" else []}

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    asyncio.run(manager._handle_server_request({
        "id": "srq-simple1", "method": "clarify",
        "params": {"session_id": "tui-1", "question": "Why?"},
    }))
    asyncio.run(manager.respond_server_request(
        browser, "chat-1", "srq-simple1", "clarify", {"answer": "because"},
    ))
    assert manager._ws.sent[-1] == {
        "jsonrpc": "2.0", "id": "srq-simple1", "result": {"answer": "because"},
    }

    batch = {
        "id": "srq-batch01", "method": "clarify",
        "params": {
            "session_id": "tui-1",
            "questions": [
                {"qid": "q1", "question": "First?"},
                {"qid": "q2", "question": "Second?"},
            ],
        },
    }
    asyncio.run(manager._handle_server_request(batch))
    assert asyncio.run(manager.lock_server_request(browser, "chat-1", "srq-batch01", "q1", "one"))["remaining"] == ["q2"]
    assert manager._server_requests["srq-batch01"].frontend_params["questions"][0]["qid"] == "q2"
    assert asyncio.run(manager.lock_server_request(browser, "chat-1", "srq-batch01", "q2", "two"))["remaining"] == []
    assert "srq-batch01" not in manager._server_requests

    asyncio.run(manager._handle_server_request({
        "id": "srq-sudo01", "method": "sudo", "params": {"session_id": "tui-1"},
    }))
    asyncio.run(manager.respond_server_request(
        browser, "chat-1", "srq-sudo01", "sudo", {"value": "private-password"},
    ))
    assert manager._ws.sent[-1]["id"] == "srq-sudo01"
    assert "private-password" not in json.dumps(manager._server_requests)


def test_revoked_profile_blocks_interactive_approval_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", None)
    manager, session = _manager_with_session()
    session.requested_profile = "online"
    browser = object()
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    monkeypatch.setattr(hermes_ws, "ws_broadcast", _noop_broadcast)
    asyncio.run(manager._handle_server_request(_approval("srq-revoke1")))
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    request = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "_request_json_rpc", request)

    result = asyncio.run(manager.respond_server_request(
        browser, "chat-1", "srq-revoke1", "approval", {"choice": "once"},
    ))

    assert result["status"] == "rejected"
    request.assert_awaited_once_with("session.close", {"session_id": "tui-1"})
    assert manager._ws.sent == []
    assert "srq-revoke1" not in manager._server_requests
    assert manager.get_session("chat-1") is None


def test_interactive_request_for_revoked_profile_is_closed_without_browser_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager, session = _manager_with_session()
    session.requested_profile = "online"
    broadcasts: list[dict[str, Any]] = []

    async def record_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    request = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "_request_json_rpc", request)
    monkeypatch.setattr(hermes_ws, "ws_broadcast", record_broadcast)

    asyncio.run(manager._handle_server_request(_approval("srq-revoke3")))

    assert broadcasts == []
    assert manager._server_requests == {}
    assert manager.get_session("chat-1") is None
    request.assert_awaited_once_with("session.close", {"session_id": "tui-1"})
    assert manager._ws.sent == [{
        "jsonrpc": "2.0",
        "id": "srq-revoke3",
        "error": {"code": -32603, "message": "Session is no longer allowed"},
    }]


def test_revoked_profile_blocks_interactive_clarify_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", None)
    manager, session = _manager_with_session()
    session.requested_profile = "online"
    browser = object()
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    monkeypatch.setattr(hermes_ws, "ws_broadcast", _noop_broadcast)
    asyncio.run(manager._handle_server_request({
        "id": "srq-revoke2",
        "method": "clarify",
        "params": {
            "session_id": "tui-1",
            "questions": [{"qid": "q1", "question": "Continue?"}],
        },
    }))
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    request = AsyncMock(return_value={"remaining": []})
    monkeypatch.setattr(manager, "_request_json_rpc", request)

    result = asyncio.run(manager.lock_server_request(browser, "chat-1", "srq-revoke2", "q1", "yes"))

    assert result == {"status": "rejected", "remaining": []}
    request.assert_awaited_once_with("session.close", {"session_id": "tui-1"})
    assert manager._ws.sent == []
    assert "srq-revoke2" not in manager._server_requests
    assert manager.get_session("chat-1") is None


def test_request_cancel_and_disconnect_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _manager_with_session()
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(hermes_ws, "ws_broadcast", fake_broadcast)
    asyncio.run(manager._handle_server_request(_approval("srq-cancel1")))
    asyncio.run(manager._dispatch_event(session, "request.cancel", {
        "session_id": "tui-1",
        "payload": {"id": "srq-cancel1", "method": "approval", "reason": "private detail"},
    }))
    asyncio.run(manager._dispatch_event(session, "request.cancel", {
        "session_id": "tui-1",
        "payload": {"id": "srq-cancel1", "method": "approval", "reason": "private detail"},
    }))
    assert "srq-cancel1" not in manager._server_requests
    assert broadcasts[-1]["reason"] == "Hermes request cancelled"
    assert "private detail" not in json.dumps(broadcasts)

    asyncio.run(manager._handle_server_request(_approval("srq-drop01")))
    asyncio.run(manager._fail_pending_operations_on_disconnect())
    assert manager._server_requests == {}
    with pytest.raises(ValueError):
        asyncio.run(manager.respond_server_request(
            object(), "chat-1", "srq-drop01", "approval", {"choice": "once"},
        ))


def test_request_cancel_is_scoped_to_rpc_id_session_and_method(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _manager_with_session()
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(hermes_ws, "ws_broadcast", fake_broadcast)
    asyncio.run(manager._handle_server_request(_approval("srq-one")))
    asyncio.run(manager._handle_server_request(_approval("srq-two")))

    cancel = {
        "session_id": "tui-1",
        "payload": {"id": "srq-one", "method": "approval", "reason": "do not forward"},
    }
    asyncio.run(manager._dispatch_event(session, "request.cancel", cancel))

    assert list(manager._server_requests) == ["srq-two"]
    cancel_events = [event for event in broadcasts if event["type"] == "server_request_cancel"]
    assert cancel_events == [{
        "type": "server_request_cancel",
        "session_id": "chat-1",
        "rpc_id": "srq-one",
        "method": "approval",
        "reason": "Hermes request cancelled",
    }]

    # A repeated cancel, a foreign TUI session, or a mismatched method cannot
    # touch the other live request or emit another browser event.
    asyncio.run(manager._dispatch_event(session, "request.cancel", cancel))
    asyncio.run(manager._dispatch_event(session, "request.cancel", {
        "session_id": "tui-foreign",
        "payload": {"id": "srq-two", "method": "approval"},
    }))
    asyncio.run(manager._dispatch_event(session, "request.cancel", {
        "session_id": "tui-1",
        "payload": {"id": "srq-two", "method": "clarify"},
    }))
    assert list(manager._server_requests) == ["srq-two"]
    assert [event for event in broadcasts if event["type"] == "server_request_cancel"] == cancel_events


def test_response_rejection_is_recoverable_and_delivery_failure_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _session = _manager_with_session()
    browser = object()
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(hermes_ws, "ws_broadcast", fake_broadcast)
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    asyncio.run(manager._handle_server_request(_approval("srq-reject1")))
    asyncio.run(manager._handle_server_request(_approval("srq-fail001")))

    rejected = asyncio.run(manager.respond_server_request(
        browser,
        "chat-1",
        "srq-reject1",
        "approval",
        {"choice": "not-offered", "secret": "must-not-escape"},
    ))
    assert rejected["status"] == "rejected"
    assert "srq-reject1" in manager._server_requests
    assert broadcasts[-1] == {
        "type": "server_request_error",
        "session_id": "chat-1",
        "rpc_id": "srq-reject1",
        "method": "approval",
        "status": "rejected",
    }
    assert "must-not-escape" not in json.dumps(broadcasts)

    async def fail_send(_rpc_id: str, _result: dict[str, Any]) -> None:
        raise OSError("transport details must not escape")

    monkeypatch.setattr(manager, "_send_server_request_response", fail_send)
    uncertain = asyncio.run(manager.respond_server_request(
        browser, "chat-1", "srq-fail001", "approval", {"choice": "once"},
    ))
    assert uncertain["status"] == "delivery_uncertain"
    assert "srq-fail001" not in manager._server_requests
    assert broadcasts[-1] == {
        "type": "server_request_error",
        "session_id": "chat-1",
        "rpc_id": "srq-fail001",
        "method": "approval",
        "status": "delivery_uncertain",
    }
    assert "transport details" not in json.dumps(broadcasts)
    with pytest.raises(ValueError):
        asyncio.run(manager.respond_server_request(
            browser, "chat-1", "srq-fail001", "approval", {"choice": "once"},
        ))


def test_clarify_lock_rejection_reactivates_the_same_request(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    browser = object()
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    async def reject_lock(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise hermes_ws.HermesJsonRpcError(400, "private Hermes error")

    monkeypatch.setattr(hermes_ws, "ws_broadcast", fake_broadcast)
    monkeypatch.setattr(hermes_ws, "ws_is_subscribed", lambda _ws, _sid: True)
    monkeypatch.setattr(manager, "_request_json_rpc", reject_lock)
    asyncio.run(manager._handle_server_request({
        "id": "srq-lock001",
        "method": "clarify",
        "params": {
            "session_id": "tui-1",
            "questions": [{"qid": "q1", "question": "First?"}],
        },
    }))

    result = asyncio.run(manager.lock_server_request(browser, "chat-1", "srq-lock001", "q1", "answer"))
    assert result["status"] == "rejected"
    assert "srq-lock001" in manager._server_requests
    assert broadcasts[-1] == {
        "type": "server_request_error",
        "session_id": "chat-1",
        "rpc_id": "srq-lock001",
        "method": "clarify",
        "status": "rejected",
    }
    assert "private Hermes error" not in json.dumps(broadcasts)

    async def timeout_lock(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise asyncio.TimeoutError()

    monkeypatch.setattr(manager, "_request_json_rpc", timeout_lock)
    uncertain = asyncio.run(manager.lock_server_request(browser, "chat-1", "srq-lock001", "q1", "private-answer"))
    assert uncertain["status"] == "delivery_uncertain"
    assert "srq-lock001" not in manager._server_requests
    assert broadcasts[-1] == {
        "type": "server_request_error",
        "session_id": "chat-1",
        "rpc_id": "srq-lock001",
        "method": "clarify",
        "status": "delivery_uncertain",
    }
    assert "private-answer" not in json.dumps(broadcasts)


def test_gateway_ready_restores_only_deduplicated_open_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    monkeypatch.setattr(hermes_ws, "ws_broadcast", _noop_broadcast)
    calls: list[str] = []

    async def fake_request(method: str, _params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        calls.append(method)
        if method == "session.events.since":
            return {"open_requests": [_approval("srq-restore1").copy(), _approval("srq-restore1").copy()], "events": [{"private": "ignored"}]}
        return {}

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    async def run() -> None:
        await manager._handle_message(json.dumps({
            "method": "event", "params": {"type": "gateway.ready", "payload": {"skin": {"name": "test"}}},
        }))
        task = manager._server_request_restore_task
        assert task is not None
        await task

    asyncio.run(run())
    assert calls.count("session.events.since") == 1
    assert list(manager._server_requests) == ["srq-restore1"]
    assert "private" not in json.dumps(manager.server_request_snapshot("chat-1"))


async def _noop_broadcast(_session_id: str, _message: dict[str, Any]) -> None:
    return None
