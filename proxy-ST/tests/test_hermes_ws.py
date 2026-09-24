from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from proxy_st import config
import proxy_st.hermes_ws as hermes_ws
from proxy_st.hermes_ws import (
    HermesJsonRpcError,
    HermesSession,
    HermesWebSocketManager,
    _persona_patch_request_from_payload,
)
from proxy_st.state import SESSION_INFOS


def _manager_with_session() -> tuple[HermesWebSocketManager, HermesSession]:
    manager = HermesWebSocketManager()
    session = HermesSession(tui_session_id="tui-1", st_session_id="chat-1")
    manager._sessions["tui-1"] = session
    manager._st_to_tui["chat-1"] = "tui-1"
    return manager, session


def _stub_prompt_session(monkeypatch: pytest.MonkeyPatch, manager: HermesWebSocketManager) -> HermesSession:
    session = HermesSession(tui_session_id="tui-1", st_session_id="chat-1")
    manager._sessions["tui-1"] = session
    manager._st_to_tui["chat-1"] = "tui-1"
    manager._should_connect = True
    manager._ready = True
    monkeypatch.setattr(manager, "ensure_session", lambda *_args, **_kwargs: _async_result("tui-1"))
    monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)
    return session


async def _async_result(value: Any) -> Any:
    return value


async def _collect_prompt(manager: HermesWebSocketManager) -> list[dict[str, Any]]:
    return [event async for event in manager.submit_prompt("chat-1", "private prompt text")]


def test_prompt_submit_ack_success_streams_early_delta_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        assert request_id in manager._rpc_waiters
        await manager._dispatch_event(session, "message.delta", {"payload": {"text": "early"}})
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        await manager._dispatch_event(
            session,
            "message.complete",
            {"payload": {"status": "complete"}},
        )

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(_collect_prompt(manager))

    assert events == [
        {"type": "text", "text": "early"},
        {"type": "done", "status": "complete", "payload": {"status": "complete"}},
    ]
    assert session.submitted_prompt_count == 1
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False
    assert session.turn_complete_event.is_set()


def test_prompt_submit_json_rpc_error_is_immediate_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    send_count = 0

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal send_count
        send_count += 1
        assert method == "prompt.submit"
        await manager._handle_response({
            "id": request_id,
            "error": {
                "code": 4090,
                "message": "session is busy",
                "data": {"prompt": "private prompt text", "context": "private context"},
            },
        })

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(asyncio.wait_for(_collect_prompt(manager), timeout=0.2))

    assert events == [{"type": "error", "message": "[Hermes] Prompt submission rejected: Hermes error 4090: session is busy"}]
    assert send_count == 1
    assert session.submitted_prompt_count == 0
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False
    assert session.turn_complete_event.is_set()
    assert "private prompt text" not in caplog.text
    assert "private context" not in caplog.text
    assert "{\"code\"" not in events[0]["message"]


def test_prompt_submit_delta_before_ack_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        await manager._dispatch_event(session, "message.delta", {"payload": {"text": "before-ack"}})
        await asyncio.sleep(0)
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        await manager._dispatch_event(session, "message.complete", {"payload": {}})

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(_collect_prompt(manager))

    assert events[0] == {"type": "text", "text": "before-ack"}
    assert events[-1]["type"] == "done"
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}


def test_prompt_submit_ack_timeout_reports_uncertain_state_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    monkeypatch.setattr(hermes_ws, "PROMPT_SUBMIT_ACK_TIMEOUT_SECONDS", 0.001)
    request_ids: list[int] = []
    send_count = 0

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal send_count
        send_count += 1
        assert method == "prompt.submit"
        request_ids.append(request_id)
        await asyncio.sleep(0)

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(asyncio.wait_for(_collect_prompt(manager), timeout=0.2))

    assert events == [{
        "type": "error",
        "message": "[Proxy] Prompt submission acknowledgement timed out; submission state is uncertain and was not retried.",
    }]
    assert send_count == 1
    assert request_ids
    assert session.submitted_prompt_count == 0
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False
    assert session.turn_complete_event.is_set()

    asyncio.run(manager._handle_response({
        "id": request_ids[0],
        "result": {"status": "streaming"},
    }))
    assert manager._rpc_waiters == {}


def test_prompt_submit_disconnect_fails_ack_waiter_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)

    async def fake_send(_request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        waiter = manager._rpc_waiters[_request_id]
        await manager._fail_pending_operations_on_disconnect()
        with pytest.raises(ConnectionError, match="closed"):
            await waiter

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(asyncio.wait_for(_collect_prompt(manager), timeout=0.2))

    assert events == [{
        "type": "error",
        "message": (
            "[Proxy] Hermes connection lost while waiting for prompt submission acknowledgement; "
            "submission state is uncertain and was not retried."
        ),
    }]
    assert session.submitted_prompt_count == 0
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False
    assert session.turn_complete_event.is_set()


def test_prompt_submit_post_ack_hermes_error_is_emitted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        await manager._dispatch_event(
            session,
            "error",
            {"payload": {"message": "provider unavailable"}},
        )

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(_collect_prompt(manager))

    assert events == [{"type": "error", "message": "provider unavailable"}]
    assert session.submitted_prompt_count == 1
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.turn_complete_event.is_set()


@pytest.mark.parametrize("status", ["queued", "steered", "redirected"])
def test_prompt_submit_non_streaming_status_is_accepted_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    send_count = 0

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal send_count
        send_count += 1
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": {"status": status}})

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(asyncio.wait_for(_collect_prompt(manager), timeout=0.2))

    assert events == [{
        "type": "accepted",
        "accepted": True,
        "retry": False,
        "status": status,
        "message": hermes_ws._PROMPT_SUBMIT_NON_STREAM_MESSAGES[status],
    }]
    assert send_count == 1
    assert session.submitted_prompt_count == 1
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.turn_complete_event.is_set()


def test_prompt_submit_voice_stop_finishes_without_stream_or_count(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": {"voice_stopped": True}})

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(asyncio.wait_for(_collect_prompt(manager), timeout=0.2))

    assert events == [{
        "type": "done",
        "status": "complete",
        "payload": {"voice_stopped": True},
    }]
    assert session.submitted_prompt_count == 0
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.turn_complete_event.is_set()


@pytest.mark.parametrize(
    ("result", "expected_message"),
    [
        ({}, "unknown acknowledgement result."),
        ({"status": "accepted"}, "unknown acknowledgement result."),
        (["streaming"], "acknowledgement is not an object."),
    ],
)
def test_prompt_submit_unknown_ack_is_protocol_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    result: Any,
    expected_message: str,
) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    send_count = 0

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal send_count
        send_count += 1
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": result})

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(asyncio.wait_for(_collect_prompt(manager), timeout=0.2))

    assert events == [{
        "type": "error",
        "message": f"[Hermes] Prompt submission protocol error: {expected_message}",
    }]
    assert send_count == 1
    assert session.submitted_prompt_count == 0
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.turn_complete_event.is_set()


def test_prompt_submit_runtime_error_after_ack_keeps_stream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)

    class ExplodingEvent(dict[str, Any]):
        def __getitem__(self, _key: str) -> Any:
            raise RuntimeError("stream event exploded")

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        session.pending_queues[request_id].put_nowait(ExplodingEvent())

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    with pytest.raises(RuntimeError, match="stream event exploded"):
        asyncio.run(_collect_prompt(manager))

    assert session.submitted_prompt_count == 1
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False


def test_prompt_submit_cancellation_during_ack_cleans_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    send_started = asyncio.Event()

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        assert request_id in manager._rpc_waiters
        send_started.set()

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def run() -> None:
        task = asyncio.create_task(_collect_prompt(manager))
        await send_started.wait()
        await asyncio.sleep(0)
        assert manager._rpc_waiters
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False


def test_disconnect_cleans_pending_session_creation_and_wakes_waiter(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HermesWebSocketManager()
    captured_creation_waiter: asyncio.Future[Any] | None = None

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal captured_creation_waiter
        assert method == "session.create"
        assert request_id in manager._pending_requests
        pending_key = next(iter(manager._pending_creations))
        assert pending_key in manager._sessions
        manager._st_to_tui["chat-pending"] = pending_key
        captured_creation_waiter = manager._creation_waiters["chat-pending"]
        await manager._fail_pending_operations_on_disconnect()
        with pytest.raises(ConnectionError, match="closed"):
            await captured_creation_waiter

    manager._should_connect = True
    manager._ready = True
    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    with pytest.raises(ConnectionError, match="closed"):
        asyncio.run(manager.ensure_session("chat-pending", cwd="/workspace", profile="local"))

    assert captured_creation_waiter is not None
    assert captured_creation_waiter.done()
    assert manager._sessions == {}
    assert manager._st_to_tui == {}
    assert manager._pending_requests == {}
    assert manager._pending_creations == {}
    assert manager._creation_waiters == {}


def test_authenticated_ws_url_uses_dashboard_ticket() -> None:
    class FakeAuthenticator:
        async def fetch_ws_ticket(self) -> str:
            return "ticket with space"

    manager = HermesWebSocketManager(
        ws_url="ws://hermes.example.test/api/ws?skin=st",
        dashboard_url="http://hermes.example.test/",
        dashboard_auth_mode="password",
        dashboard_auth_username="proxy-user",
        dashboard_auth_password="proxy-pass",
        dashboard_authenticator=FakeAuthenticator(),
    )

    ws_url = asyncio.run(manager._authenticated_ws_url())

    assert ws_url == "ws://hermes.example.test/api/ws?skin=st&ticket=ticket%20with%20space"


def test_has_prompt_history_requires_submitted_prompt() -> None:
    manager, session = _manager_with_session()

    assert manager.has_prompt_history("chat-1") is False

    session.submitted_prompt_count = 1
    session.mapping_needs_validation = True

    assert manager.has_prompt_history("chat-1") is True


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HermesJsonRpcError(4001, "session not found"), True),
        (HermesJsonRpcError(4001, "another validation error"), False),
        (HermesJsonRpcError(5000, "session not found"), False),
        (RuntimeError('{"code": 4001, "message": "session not found"}'), True),
        (RuntimeError('{"error": {"code": "4001", "message": "session not found"}}'), True),
        (RuntimeError("Hermes error 4001: session not found"), True),
        (RuntimeError('{"code": 4001, "message": "session missing"}'), False),
        (RuntimeError('{"code": 5000, "message": "session not found"}'), False),
        (RuntimeError('{"code": 4001, "message": "session not found while validating"}'), False),
        (RuntimeError("code=4001; session not found"), False),
        (RuntimeError("session"), False),
    ],
)
def test_session_not_found_classification_is_strict(
    error: BaseException,
    expected: bool,
) -> None:
    assert HermesWebSocketManager._is_session_not_found_error(error) is expected


def test_undo_session_decrements_prompt_history(monkeypatch: Any) -> None:
    manager, session = _manager_with_session()
    session.submitted_prompt_count = 2

    async def fake_request_json_rpc(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request_json_rpc)

    result = asyncio.run(manager.undo_session("chat-1"))

    assert result == {"ok": True}
    assert session.submitted_prompt_count == 1


def test_undo_session_invalidates_expired_mapping(monkeypatch: Any) -> None:
    manager, _session = _manager_with_session()

    async def fake_request_json_rpc(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        raise RuntimeError('{"code": 4001, "message": "session not found"}')

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request_json_rpc)

    result = asyncio.run(manager.undo_session("chat-1"))

    assert result == {"removed": 0, "session_expired": True}
    assert manager.get_session("chat-1") is None
    assert "chat-1" not in manager._st_to_tui


def test_compress_session_is_noop_without_live_session() -> None:
    manager = HermesWebSocketManager()

    result = asyncio.run(manager.compress_session("chat-1"))

    assert result["skipped"] is True
    assert result["session_expired"] is True


def test_session_status_distinguishes_live_and_expired_sessions() -> None:
    manager, session = _manager_with_session()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()

    assert manager.session_status("chat-1") == "active"

    session.is_processing = True
    assert manager.session_status("chat-1") == "working"

    manager._invalidate_session("chat-1", reason="test")
    SESSION_INFOS["chat-1"] = {"hermes": {"tui_session_id": "tui-1"}}
    try:
        assert manager.session_status("chat-1") == "expired"
        assert manager.session_status("new-chat") == "not_started"
    finally:
        SESSION_INFOS.pop("chat-1", None)


def test_usage_probe_invalidates_expired_session(monkeypatch: Any) -> None:
    manager, _session = _manager_with_session()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()

    async def fake_request_json_rpc(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        raise RuntimeError('{"code": 4001, "message": "session not found"}')

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request_json_rpc)

    result = asyncio.run(manager.get_session_usage("chat-1"))

    assert result is None
    assert manager.get_session("chat-1") is None


@pytest.mark.parametrize("failure", [asyncio.TimeoutError(), ConnectionError("connection lost")])
def test_usage_probe_ambiguous_failure_keeps_mapping_without_prompt_or_create(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    manager, session = _manager_with_session()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    session.mapping_needs_validation = True
    calls: list[str] = []

    async def fake_request(method: str, *_args: Any, **_kwargs: Any) -> Any:
        calls.append(method)
        assert method == "session.usage"
        raise failure

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)

    async def consume() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt("chat-1", "current prompt")]

    with pytest.raises(type(failure)):
        asyncio.run(consume())

    assert calls == ["session.usage"]
    assert manager.get_session("chat-1") is session
    assert session.mapping_needs_validation is True
    assert session.submitted_prompt_count == 0
    assert manager._pending_creations == {}
    assert manager._rpc_waiters == {}


async def _run_concurrent_mapping_validation(manager: HermesWebSocketManager) -> None:
    first = asyncio.create_task(manager.ensure_session("chat-1"))
    await asyncio.sleep(0)
    second = asyncio.create_task(manager.ensure_session("chat-1"))
    await asyncio.gather(first, second)


def test_reconnect_validation_probe_is_shared_between_concurrent_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session = _manager_with_session()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    calls: list[str] = []

    async def fake_request(method: str, *_args: Any, **_kwargs: Any) -> dict[str, int]:
        calls.append(method)
        if method == "session.usage":
            probe_started.set()
            await release_probe.wait()
            return {"total": 1}
        return {}

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: asyncio.sleep(0))

    async def run() -> None:
        await manager._handle_message(json.dumps({
            "method": "event",
            "params": {"type": "gateway.ready", "payload": {"skin": {"name": "test"}}},
        }))
        validation = asyncio.create_task(_run_concurrent_mapping_validation(manager))
        await probe_started.wait()
        release_probe.set()
        await validation

    asyncio.run(run())

    assert calls.count("session.usage") == 1
    assert calls.count("session.create") == 0
    assert manager.get_session("chat-1") is session
    assert session.mapping_needs_validation is False


def test_reconnect_keeps_live_mapping_after_one_lazy_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _manager_with_session()
    session.requested_cwd = "/workspace"
    session.requested_profile = "local"
    session.submitted_prompt_count = 1
    manager._should_connect = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    calls: list[str] = []

    async def fake_request(method: str, *_args: Any, **_kwargs: Any) -> dict[str, int]:
        calls.append(method)
        return {"total": 3}

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: asyncio.sleep(0))

    async def run() -> tuple[str | None, str | None]:
        await manager._handle_message(json.dumps({
            "method": "event",
            "params": {"type": "gateway.ready", "payload": {"skin": {"name": "test"}}},
        }))
        first = await manager.ensure_session("chat-1", cwd="/workspace", profile="local")
        second = await manager.ensure_session("chat-1", cwd="/workspace", profile="local")
        return first, second

    first, second = asyncio.run(run())

    assert first == second == "tui-1"
    assert manager.get_session("chat-1") is session
    assert calls.count("session.usage") == 1
    assert "session.create" not in calls
    assert session.mapping_needs_validation is False
    assert manager.has_prompt_history("chat-1") is True


def test_reconnect_lost_mapping_rebuilds_with_full_context_and_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session = _manager_with_session()
    session.requested_cwd = "/old-workspace"
    session.requested_profile = "online"
    session.requested_model = "old-model"
    session.info = {"model": "old-model"}
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    manager._configure_tool_progress_mode = lambda _session: asyncio.sleep(0)  # type: ignore[method-assign]
    session.mapping_needs_validation = True
    captured: dict[str, Any] = {}

    async def fake_request(method: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if method == "session.usage":
            raise HermesJsonRpcError(4001, "session not found")
        return {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        captured.update(method=method, params=params)
        assert method == "session.create"
        pending_key = next(iter(manager._pending_creations))
        new_session = manager._sessions.pop(pending_key)
        new_session.info = {"model": params.get("model")}
        new_session.tui_session_id = "tui-rebuilt"
        manager._sessions["tui-rebuilt"] = new_session
        manager._st_to_tui["chat-1"] = "tui-rebuilt"
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["chat-1"].set_result("tui-rebuilt")

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)
    monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)

    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    result = asyncio.run(manager.ensure_session(
        "chat-1",
        cwd="/workspace",
        profile="local",
        model="new-model",
        messages=history,
        system_context="system",
        persona_context="persona",
        persona_reminder="reminder",
        persona_version="v2",
    ))

    assert result == "tui-rebuilt"
    assert manager.get_session("chat-1") is not session
    assert captured == {
        "method": "session.create",
        "params": {
            "cols": 80,
            "source": "sillytavern",
            "cwd": "/workspace",
            "profile": "local",
            "model": "new-model",
            "messages": history,
            "system_context": "system",
            "persona_context": "persona",
            "persona_reminder": "reminder",
            "persona_version": "v2",
        },
    }
    assert manager._pending_requests == {}
    assert manager._pending_creations == {}
    assert manager._creation_waiters == {}


def test_stale_mapping_validated_before_image_attach_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, old_session = _manager_with_session()
    old_session.mapping_needs_validation = True
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: asyncio.sleep(0))
    monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)
    operations: list[str] = []
    created_params: dict[str, Any] = {}

    async def fake_request(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        operations.append(method)
        if method == "session.usage":
            raise HermesJsonRpcError(4001, "session not found")
        assert method == "image.attach_bytes"
        assert params["session_id"] == "tui-attach"
        return {"attached": True}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        operations.append(method)
        if method == "session.create":
            created_params.update(params)
            pending_key = next(iter(manager._pending_creations))
            new_session = manager._sessions.pop(pending_key)
            new_session.info = {"model": params.get("model")}
            new_session.tui_session_id = "tui-attach"
            manager._sessions["tui-attach"] = new_session
            manager._st_to_tui["chat-1"] = "tui-attach"
            manager._pending_creations.pop(pending_key)
            manager._pending_requests.pop(request_id)
            manager._creation_waiters["chat-1"].set_result("tui-attach")
            return
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        await manager._dispatch_event(
            manager.get_session("chat-1"),
            "message.complete",
            {"payload": {"status": "complete"}},
        )

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def run() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt(
            "chat-1",
            "describe this",
            [{"content_base64": "aW1hZ2U=", "filename": "test.png"}],
            conversation_history=[{"role": "user", "content": "earlier"}],
            system_context="system",
            persona_context="persona",
            persona_reminder="reminder",
            persona_version="v2",
            workspace_cwd="/workspace",
            profile="local",
            model="model-a",
        )]

    events = asyncio.run(run())

    assert operations == ["session.usage", "session.create", "image.attach_bytes", "prompt.submit"]
    assert created_params["messages"] == [{"role": "user", "content": "earlier"}]
    assert created_params["cwd"] == "/workspace"
    assert created_params["profile"] == "local"
    assert created_params["model"] == "model-a"
    assert created_params["system_context"] == "system"
    assert created_params["persona_context"] == "persona"
    assert created_params["persona_reminder"] == "reminder"
    assert created_params["persona_version"] == "v2"
    assert events == [{"type": "done", "status": "complete", "payload": {"status": "complete"}}]
    session = manager.get_session("chat-1")
    assert session is not None
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.turn_complete_event.is_set()


def test_prompt_session_not_found_rebuilds_once_without_duplicate_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: asyncio.sleep(0))
    monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)
    prompt_calls = 0
    create_calls = 0

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        nonlocal prompt_calls, create_calls
        if method == "prompt.submit":
            prompt_calls += 1
            if prompt_calls == 1:
                await manager._handle_response({
                    "id": request_id,
                    "error": {"code": 4001, "message": "session not found", "data": {"prompt": "secret"}},
                })
                return
            await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
            await manager._dispatch_event(
                manager.get_session("chat-1"),
                "message.complete",
                {"payload": {"status": "complete"}},
            )
            return
        assert method == "session.create"
        create_calls += 1
        pending_key = next(iter(manager._pending_creations))
        new_session = manager._sessions.pop(pending_key)
        new_session.tui_session_id = "tui-retry"
        manager._sessions["tui-retry"] = new_session
        manager._st_to_tui["chat-1"] = "tui-retry"
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["chat-1"].set_result("tui-retry")

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def run() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt("chat-1", "one prompt")]

    events = asyncio.run(asyncio.wait_for(run(), timeout=0.5))

    assert prompt_calls == 2
    assert create_calls == 1
    assert events == [{"type": "done", "status": "complete", "payload": {"status": "complete"}}]
    assert manager.get_session("chat-1").submitted_prompt_count == 1
    assert manager._rpc_waiters == {}


def test_prompt_submit_non_stale_4001_is_final_without_rebuild_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    prompt_calls = 0
    create_calls = 0

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal prompt_calls, create_calls
        if method == "prompt.submit":
            prompt_calls += 1
            await manager._handle_response({
                "id": request_id,
                "error": {"code": 4001, "message": "another validation error"},
            })
            return
        create_calls += 1
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    events = asyncio.run(_collect_prompt(manager))

    assert prompt_calls == 1
    assert create_calls == 0
    assert events == [{
        "type": "error",
        "message": "[Hermes] Prompt submission rejected: Hermes error 4001: another validation error",
    }]
    assert manager.get_session("chat-1") is session
    assert session.submitted_prompt_count == 0


def test_image_attach_non_stale_4001_is_final_without_rebuild_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HermesWebSocketManager()
    session = _stub_prompt_session(monkeypatch, manager)
    image_calls = 0
    prompt_calls = 0
    create_calls = 0

    async def fake_request(method: str, *_args: Any, **_kwargs: Any) -> Any:
        nonlocal image_calls, prompt_calls, create_calls
        if method == "image.attach_bytes":
            image_calls += 1
            raise HermesJsonRpcError(4001, "another validation error")
        if method == "prompt.submit":
            prompt_calls += 1
        if method == "session.create":
            create_calls += 1
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)

    async def run() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt(
            "chat-1",
            "current prompt",
            [{"content_base64": "aW1hZ2U=", "filename": "test.png"}],
        )]

    events = asyncio.run(run())

    assert image_calls == 1
    assert prompt_calls == 0
    assert create_calls == 0
    assert events == [{"type": "error", "message": "Image attachment failed: Hermes error 4001: another validation error"}]
    assert manager.get_session("chat-1") is session


def test_prompt_second_session_not_found_is_final_without_third_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _session = _manager_with_session()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: asyncio.sleep(0))
    monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)
    prompt_calls = 0
    create_calls = 0

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        nonlocal prompt_calls, create_calls
        if method == "prompt.submit":
            prompt_calls += 1
            await manager._handle_response({
                "id": request_id,
                "error": {
                    "code": 4001,
                    "message": "session not found",
                    "data": {"prompt": "private prompt", "context": "private context"},
                },
            })
            return
        assert method == "session.create"
        create_calls += 1
        pending_key = next(iter(manager._pending_creations))
        new_session = manager._sessions.pop(pending_key)
        new_session.tui_session_id = f"tui-retry-{create_calls}"
        manager._sessions[new_session.tui_session_id] = new_session
        manager._st_to_tui["chat-1"] = new_session.tui_session_id
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["chat-1"].set_result(new_session.tui_session_id)

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def run() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt("chat-1", "one prompt")]

    events = asyncio.run(asyncio.wait_for(run(), timeout=0.5))

    assert prompt_calls == 2
    assert create_calls == 1
    assert len(events) == 1
    assert "session not found" in events[0]["message"]
    assert "private prompt" not in events[0]["message"]
    assert "private context" not in events[0]["message"]
    assert manager.get_session("chat-1") is None
    assert manager._rpc_waiters == {}


def test_model_triggered_session_creation_keeps_workspace_and_profile(monkeypatch: Any) -> None:
    manager = HermesWebSocketManager()
    captured: dict[str, Any] = {}

    async def fake_ensure(st_session_id: str, **kwargs: Any) -> str:
        captured.update({"session_id": st_session_id, **kwargs})
        return "tui-1"

    async def fake_request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured.update({"method": method, "params": params})
        return {"value": "model-a"}

    monkeypatch.setattr(manager, "ensure_session", fake_ensure)
    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)

    result = asyncio.run(manager.set_model("chat-1", "model-a", cwd="/", profile="local"))

    assert result["value"] == "model-a"
    assert captured["session_id"] == "chat-1"
    assert captured["cwd"] == "/"
    assert captured["profile"] == "local"
    assert captured["method"] == "config.set"


def test_disallowed_explicit_profile_fails_before_session_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager = HermesWebSocketManager()
    send = AsyncMock()
    monkeypatch.setattr(manager, "_send_json_rpc", send)

    with pytest.raises(hermes_ws.ProfileNotAllowedError) as exc_info:
        asyncio.run(manager.ensure_session("chat-rejected", profile="online"))

    assert str(exc_info.value) == "Hermes profile is not allowed"
    send.assert_not_awaited()


def test_revoked_live_profile_migrates_to_default_before_next_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    monkeypatch.setattr(hermes_ws, "_save_sessions", lambda: None)
    manager, old_session = _manager_with_session()
    old_session.requested_profile = "default"
    old_session.info = {"profile_name": "online", "model": "model-a"}
    old_session.mapping_needs_validation = True
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_validate_session_mapping", lambda *_args: _async_result(True))
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: _async_result(None))
    operations: list[tuple[str, dict[str, Any]]] = []

    async def fake_request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        operations.append((method, params))
        return {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        operations.append((method, params))
        if method == "session.create":
            pending_key = next(iter(manager._pending_creations))
            new_session = manager._sessions.pop(pending_key)
            new_session.info = {"model": params.get("model")}
            new_session.tui_session_id = "tui-default"
            manager._sessions[new_session.tui_session_id] = new_session
            manager._st_to_tui["chat-1"] = new_session.tui_session_id
            manager._pending_creations.pop(pending_key)
            manager._pending_requests.pop(request_id)
            manager._creation_waiters["chat-1"].set_result(new_session.tui_session_id)
            return
        assert method == "prompt.submit"
        assert params["session_id"] == "tui-default"
        session = manager.get_session("chat-1")
        assert session is not None
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        await manager._dispatch_event(session, "message.complete", {"payload": {"status": "complete"}})

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def run() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt("chat-1", "next turn")]

    events = asyncio.run(run())

    assert [method for method, _params in operations] == ["session.close", "session.create", "prompt.submit"]
    create_params = operations[1][1]
    assert create_params["profile"] == "default"
    assert events[-1]["type"] == "done"
    assert old_session.tui_session_id == "tui-1"
    assert manager.get_session("chat-1") is not old_session
    assert manager.get_session("chat-1").requested_profile == "default"


def test_disallowed_persisted_profile_is_cleared_before_session_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    monkeypatch.setattr(hermes_ws, "_save_sessions", lambda: None)
    info = {"profile": "local", "hermes": {"profile_name": "local", "model": "model-a"}}
    monkeypatch.setitem(SESSION_INFOS, "chat-restored", info)
    manager = HermesWebSocketManager()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: _async_result(None))
    created: dict[str, Any] = {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        assert method == "session.create"
        created.update(params)
        pending_key = next(iter(manager._pending_creations))
        session = manager._sessions.pop(pending_key)
        session.tui_session_id = "tui-restored-default"
        manager._sessions[session.tui_session_id] = session
        manager._st_to_tui["chat-restored"] = session.tui_session_id
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["chat-restored"].set_result(session.tui_session_id)

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    result = asyncio.run(manager.ensure_session("chat-restored", messages=[]))

    assert result == "tui-restored-default"
    assert created["profile"] == "default"
    assert "profile" not in info
    assert info["hermes"] == {"model": "model-a"}
    assert manager.get_session("chat-restored").requested_profile == "default"


def test_implicit_restricted_session_create_selects_default_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    monkeypatch.setattr(hermes_ws, "_save_sessions", lambda: None)
    manager = HermesWebSocketManager()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: _async_result(None))
    created: dict[str, Any] = {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        assert method == "session.create"
        created.update(params)
        pending_key = next(iter(manager._pending_creations))
        session = manager._sessions.pop(pending_key)
        session.tui_session_id = "tui-quickstart"
        manager._sessions[session.tui_session_id] = session
        manager._st_to_tui["quickstart"] = session.tui_session_id
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["quickstart"].set_result(session.tui_session_id)

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    result = asyncio.run(manager.ensure_session("quickstart", messages=[]))

    assert result == "tui-quickstart"
    assert created["profile"] == "default"
    assert manager.get_session("quickstart").requested_profile == "default"


def test_allowed_persisted_profile_is_restored_for_session_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default", "local"}))
    monkeypatch.setattr(hermes_ws, "_save_sessions", lambda: None)
    info = {"profile": "local", "hermes": {"profile_name": "local", "model": "model-a"}}
    monkeypatch.setitem(SESSION_INFOS, "chat-restored-allowed", info)
    manager = HermesWebSocketManager()
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: _async_result(None))
    created: dict[str, Any] = {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        assert method == "session.create"
        created.update(params)
        pending_key = next(iter(manager._pending_creations))
        session = manager._sessions.pop(pending_key)
        session.tui_session_id = "tui-restored-local"
        manager._sessions[session.tui_session_id] = session
        manager._st_to_tui["chat-restored-allowed"] = session.tui_session_id
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["chat-restored-allowed"].set_result(session.tui_session_id)

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    result = asyncio.run(manager.ensure_session("chat-restored-allowed", messages=[]))

    assert result == "tui-restored-local"
    assert created["profile"] == "local"
    assert manager.get_session("chat-restored-allowed").requested_profile == "local"


def test_set_profile_default_is_allowed_without_hermes_profile_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    monkeypatch.setattr(hermes_ws, "_save_sessions", lambda: None)
    monkeypatch.setitem(SESSION_INFOS, "chat-default", {})
    manager = HermesWebSocketManager()
    close_session = AsyncMock()
    profile_lookup = AsyncMock()
    monkeypatch.setattr(manager, "close_session", close_session)
    monkeypatch.setattr(manager, "profile_options", profile_lookup)

    result = asyncio.run(manager.set_profile("chat-default", "default"))

    assert result == {"profile": "default", "rebuild_required": True}
    assert SESSION_INFOS["chat-default"]["profile"] == "default"
    close_session.assert_awaited_once_with("chat-default")
    profile_lookup.assert_not_awaited()


def test_unlabeled_live_session_migrates_to_implicit_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    monkeypatch.setattr(hermes_ws, "_save_sessions", lambda: None)
    manager, old_session = _manager_with_session()
    old_session.info = {"model": "model-a"}
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    monkeypatch.setattr(manager, "_validate_session_mapping", lambda *_args: _async_result(True))
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: _async_result(None))
    operations: list[tuple[str, dict[str, Any]]] = []

    async def fake_request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        operations.append((method, params))
        return {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        operations.append((method, params))
        if method == "session.create":
            pending_key = next(iter(manager._pending_creations))
            new_session = manager._sessions.pop(pending_key)
            new_session.tui_session_id = "tui-default"
            manager._sessions[new_session.tui_session_id] = new_session
            manager._st_to_tui["chat-1"] = new_session.tui_session_id
            manager._pending_creations.pop(pending_key)
            manager._pending_requests.pop(request_id)
            manager._creation_waiters["chat-1"].set_result(new_session.tui_session_id)
            return
        assert method == "prompt.submit"
        session = manager.get_session("chat-1")
        assert session is not None
        await manager._handle_response({"id": request_id, "result": {"status": "streaming"}})
        await manager._dispatch_event(session, "message.complete", {"payload": {"status": "complete"}})

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)
    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def run() -> list[dict[str, Any]]:
        return [event async for event in manager.submit_prompt("chat-1", "next turn")]

    events = asyncio.run(run())

    assert [method for method, _params in operations] == ["session.close", "session.create", "prompt.submit"]
    assert operations[1][1]["profile"] == "default"
    assert manager.get_session("chat-1").requested_profile == "default"
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize("action", ["interrupt", "steer", "undo", "compress"])
def test_revoked_session_rejects_direct_action_commands(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager, session = _manager_with_session()
    session.requested_profile = "online"
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(manager, "_request_json_rpc", request)

    async def run_action() -> Any:
        if action == "interrupt":
            return await manager.interrupt_session("chat-1")
        if action == "steer":
            return await manager.steer_session("chat-1", "new instruction")
        if action == "undo":
            return await manager.undo_session("chat-1")
        return await manager.compress_session("chat-1", "topic")

    with pytest.raises(hermes_ws.ProfileNotAllowedError):
        asyncio.run(run_action())
    request.assert_not_awaited()
    assert manager.get_session("chat-1") is session


def test_revoked_session_can_still_be_closed_and_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HERMES_PROFILE_ALLOWLIST", frozenset({"default"}))
    manager, session = _manager_with_session()
    session.requested_profile = "online"
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()
    request = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "_request_json_rpc", request)

    asyncio.run(manager.close_session("chat-1"))

    request.assert_awaited_once_with("session.close", {"session_id": "tui-1"})
    assert manager.get_session("chat-1") is None
    assert "chat-1" not in manager._st_to_tui


def test_set_model_skips_redundant_live_switch(monkeypatch: Any) -> None:
    manager, session = _manager_with_session()
    session.info = {"model": "gemma4-26b"}
    requested = False

    async def fake_ensure(*_args: Any, **_kwargs: Any) -> str:
        return "tui-1"

    async def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal requested
        requested = True
        return {"value": "gemma4-26b"}

    monkeypatch.setattr(manager, "ensure_session", fake_ensure)
    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)

    result = asyncio.run(manager.set_model("chat-1", "gemma4-26b"))

    assert result == {"key": "model", "value": "gemma4-26b", "unchanged": True}
    assert requested is False


def test_interrupt_waits_for_hermes_terminal_event(monkeypatch: Any) -> None:
    manager, session = _manager_with_session()
    session.turn_complete_event.clear()
    terminal_event_received = False

    async def fake_request(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        async def finish_turn() -> None:
            nonlocal terminal_event_received
            await asyncio.sleep(0)
            terminal_event_received = True
            session.turn_complete_event.set()

        asyncio.create_task(finish_turn())
        return {"status": "interrupted"}

    monkeypatch.setattr(manager, "_request_json_rpc", fake_request)

    result = asyncio.run(manager.interrupt_session("chat-1"))

    assert result == {"status": "interrupted"}
    assert terminal_event_received is True


def test_ensure_session_drops_live_session_when_routing_changes(monkeypatch: Any) -> None:
    manager, session = _manager_with_session()
    session.requested_cwd = "/srv/hermes-workspaces/demo"
    session.requested_profile = None
    closed: list[str] = []

    async def fake_close(st_session_id: str) -> None:
        closed.append(st_session_id)
        manager._st_to_tui.pop(st_session_id, None)
        manager._sessions.pop("tui-1", None)

    monkeypatch.setattr(manager, "close_session", fake_close)

    result = asyncio.run(manager.ensure_session("chat-1", cwd="/", profile="online"))

    assert result is None
    assert closed == ["chat-1"]


def test_ensure_session_creates_with_native_history_and_stable_context(monkeypatch: Any) -> None:
    manager = HermesWebSocketManager()
    manager._should_connect = True
    manager._ready = True
    captured: dict[str, Any] = {}

    async def fake_send(request_id: int, method: str, params: dict[str, Any]) -> None:
        captured.update(method=method, params=params)
        pending_key = next(iter(manager._pending_creations))
        session = manager._sessions.pop(pending_key)
        session.info = {"model": params.get("model")}
        session.tui_session_id = "tui-new"
        manager._sessions["tui-new"] = session
        manager._st_to_tui["chat-1"] = "tui-new"
        manager._pending_creations.pop(pending_key)
        manager._pending_requests.pop(request_id)
        manager._creation_waiters["chat-1"].set_result("tui-new")

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)
    monkeypatch.setattr(manager, "_configure_tool_progress_mode", lambda _session: asyncio.sleep(0))

    async def run() -> tuple[str | None, dict[str, Any]]:
        tui_id = await manager.ensure_session(
            "chat-1",
            cwd="/workspace",
            profile="online",
            model="gemma4-26b",
            messages=[{"role": "user", "content": "earlier"}],
            system_context="response contract",
            persona_context="# ARIA",
            persona_reminder="Answer as ARIA.",
            persona_version="persona-v1",
        )
        model_result = await manager.set_model("chat-1", "gemma4-26b")
        return tui_id, model_result

    tui_id, model_result = asyncio.run(run())

    assert tui_id == "tui-new"
    assert captured["method"] == "session.create"
    assert captured["params"] == {
        "cols": 80,
        "source": "sillytavern",
        "cwd": "/workspace",
        "profile": "online",
        "model": "gemma4-26b",
        "messages": [{"role": "user", "content": "earlier"}],
        "system_context": "response contract",
        "persona_context": "# ARIA",
        "persona_reminder": "Answer as ARIA.",
        "persona_version": "persona-v1",
    }
    assert model_result == {
        "key": "model",
        "value": "gemma4-26b",
        "unchanged": True,
    }


def test_persona_patch_request_from_payload() -> None:
    result = _persona_patch_request_from_payload({
        "description": "Updated ARIA persona",
        "summary": "ARIA becomes more direct",
        "reason": "User preference",
        "request_id": "req-1",
    })

    assert result is not None
    assert result["content"] == "Updated ARIA persona"
    assert result["summary"] == "ARIA becomes more direct"
    assert result["reason"] == "User preference"
    assert result["request_id"] == "req-1"


def test_persona_patch_request_rejects_empty_payload() -> None:
    assert _persona_patch_request_from_payload({"description": "   "}) is None
