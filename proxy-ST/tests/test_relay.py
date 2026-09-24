from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from proxy_st import relay
from proxy_st.config import BACKEND_CONFIGS, DEFAULT_BACKEND, MODELS
from proxy_st.hermes_ws import HermesSession, HermesWebSocketManager
from proxy_st.request_transform import configured_backend_name
from proxy_st.state import SESSION_INFOS, SESSION_TOOL_CALLS
from proxy_st import tool_calls


@pytest.fixture(autouse=True)
def reset_session_state():
    SESSION_INFOS.clear()
    SESSION_TOOL_CALLS.clear()
    try:
        yield
    finally:
        SESSION_INFOS.clear()
        SESSION_TOOL_CALLS.clear()


def test_only_hermes_runtime_backend_is_configured(monkeypatch: Any) -> None:
    assert DEFAULT_BACKEND == "hermes"
    assert set(BACKEND_CONFIGS) == {"hermes"}
    assert "hermes" in MODELS
    assert configured_backend_name("unsupported") is None
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "base_url", "")
    monkeypatch.setitem(BACKEND_CONFIGS["hermes"], "ws_url", "ws://localhost/api/ws")
    assert configured_backend_name("hermes") == "hermes"
    assert configured_backend_name("unknown") == "hermes"


def test_unsupported_backend_request_is_rejected() -> None:
    entry = {"request_id": "req-1", "summary": {"backend": "unsupported"}}
    response = asyncio.run(relay.forward_to_backend(entry, {}, None))

    assert response is not None
    assert response.status_code == 400
    assert b"unsupported_backend" in response.body


def test_aiter_with_idle_heartbeat_emits_heartbeat_before_late_chunk() -> None:
    async def source():
        yield "first"
        await asyncio.sleep(0.03)
        yield "second"

    async def run() -> list[tuple[str, Any]]:
        items = []
        async for item in relay._aiter_with_idle_heartbeat(
            source(),
            idle_timeout=1.0,
            heartbeat_interval=0.01,
            session_id="chat-1",
            source="test",
        ):
            items.append(item)
        return items

    items = asyncio.run(run())
    assert items[0] == ("data", "first")
    assert items[-1] == ("data", "second")
    assert ("heartbeat", None) in items


def test_aiter_with_idle_heartbeat_closes_source_after_early_exit() -> None:
    closed = asyncio.Event()

    async def source():
        try:
            yield "only"
            await asyncio.sleep(30)
        finally:
            closed.set()

    async def run() -> bool:
        stream = relay._aiter_with_idle_heartbeat(
            source(),
            idle_timeout=10.0,
            heartbeat_interval=10.0,
            session_id="chat-1",
            source="test",
        )
        async for item in stream:
            assert item == ("data", "only")
            break
        await stream.aclose()
        return closed.is_set()

    assert asyncio.run(run()) is True


def test_aiter_with_idle_heartbeat_tolerates_iterator_without_aclose() -> None:
    class NoCloseIterator:
        def __init__(self) -> None:
            self._done = False

        def __aiter__(self) -> "NoCloseIterator":
            return self

        async def __anext__(self) -> str:
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return "only"

    async def run() -> list[tuple[str, Any]]:
        return [
            item
            async for item in relay._aiter_with_idle_heartbeat(
                NoCloseIterator(),
                idle_timeout=1.0,
                heartbeat_interval=0.0,
                session_id="chat-1",
                source="test",
            )
        ]

    assert asyncio.run(run()) == [("data", "only")]


def test_sillytavern_integration_context_names_native_mcp_tool() -> None:
    context = relay._sillytavern_integration_context("chat-1")

    assert "Current SillyTavern session_id: chat-1" in context


def test_sillytavern_integration_context_disambiguates_participants() -> None:
    context = relay._sillytavern_integration_context(
        "chat-1",
        user_name="Example User",
        character_name="Hermes",
    )

    assert 'USER: "Example User"' in context
    assert 'ASSISTANT CHARACTER: "Hermes"' in context
    assert "must never become the assistant's identity" in context
    assert "mcp_sillytavern_sillytavern_update_persona_description" in context
    assert "not memory or skill updates" in context
    assert 'operation="append"' in context
    assert 'operation="replace"' in context
    assert "not callable through Python globals" in context
    assert "Do not claim the native tool does not exist based on CLI or terminal output" in context
    assert "stale guidance" in context
    assert "native tool call first" in context
    assert "Fallback path only after a direct native tool call" in context
    assert "http://127.0.0.1:8010/mcp" in context
    assert "Do not use `hermes tools` or `hermes mcp call`" in context


def test_sillytavern_integration_context_explains_docker_workspace_mapping() -> None:
    context = relay._sillytavern_integration_context("chat-1", "/")

    assert "selected host workspace is '/'" in context
    assert "bind-mount that directory at `/workspace`" in context
    assert "`/var/example.txt` is available as `/workspace/var/example.txt`" in context


def test_interrupt_hermes_after_client_cancel_calls_manager() -> None:
    class _Manager:
        def __init__(self) -> None:
            self.interrupted: list[str] = []

        async def interrupt_session(self, session_id: str) -> dict[str, str]:
            self.interrupted.append(session_id)
            return {"status": "interrupted"}

    async def run() -> tuple[_Manager, bool]:
        manager = _Manager()
        result = await relay._interrupt_hermes_after_client_cancel(manager, "chat-1")
        return manager, result

    manager, result = asyncio.run(run())
    assert manager.interrupted == ["chat-1"]
    assert result is True


def test_interrupt_hermes_after_client_cancel_ignores_missing_session() -> None:
    class _Manager:
        async def interrupt_session(self, session_id: str) -> None:
            raise ValueError(f"No active session: {session_id}")

    assert asyncio.run(relay._interrupt_hermes_after_client_cancel(_Manager(), "missing-chat")) is False


def test_interrupt_hermes_after_client_cancel_requires_success_confirmation() -> None:
    class _Manager:
        async def interrupt_session(self, _session_id: str) -> dict[str, Any]:
            return {"status": "not_interrupted", "interrupted": False}

    assert asyncio.run(relay._interrupt_hermes_after_client_cancel(_Manager(), "chat-1")) is False


def test_interrupt_hermes_after_client_cancel_timeout_cancels_and_drains_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _Manager:
        async def interrupt_session(self, _session_id: str) -> dict[str, str]:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def run() -> None:
        monkeypatch.setattr(relay, "_CLIENT_CANCEL_INTERRUPT_TIMEOUT_SECONDS", 0.01)
        task = asyncio.create_task(relay._interrupt_hermes_after_client_cancel(_Manager(), "chat-1"))
        await started.wait()
        assert await task is False
        assert cancelled.is_set()
        await asyncio.sleep(0)
        assert [item for item in asyncio.all_tasks() if item is not asyncio.current_task()] == []

    asyncio.run(run())


def test_client_cancel_cleanup_persists_and_broadcasts_inactive_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "cancelled-chat"
    SESSION_INFOS.pop(session_id, None)
    saves = 0
    broadcasts: list[dict[str, Any]] = []

    class _Manager:
        async def interrupt_session(self, _session_id: str) -> dict[str, str]:
            return {"status": "interrupted"}

    def fake_save() -> None:
        nonlocal saves
        saves += 1

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(tool_calls, "_save_sessions", fake_save)
    monkeypatch.setattr(relay, "ws_broadcast", fake_broadcast)

    asyncio.run(relay._finish_client_cancel_cleanup(_Manager(), session_id))

    assert SESSION_INFOS[session_id]["agent_status"] == {"active": False}
    assert saves == 1
    assert broadcasts == [
        {
            "type": "agent_status",
            "session_id": session_id,
            "status": {"active": False},
        }
    ]
    SESSION_INFOS.pop(session_id, None)


def test_client_cancel_cleanup_failure_is_redacted_and_does_not_publish_inactive(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = "failed-cancel-chat"
    SESSION_INFOS.pop(session_id, None)
    SESSION_TOOL_CALLS[session_id] = [{"id": "tool-1", "status": "running"}]
    broadcasts: list[dict[str, Any]] = []
    callback_calls = 0

    class _Manager:
        async def interrupt_session(self, _session_id: str) -> dict[str, str]:
            raise RuntimeError("private prompt token and clarify response")

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    async def on_interrupt_confirmed() -> None:
        nonlocal callback_calls
        callback_calls += 1

    monkeypatch.setattr(relay, "ws_broadcast", fake_broadcast)

    assert asyncio.run(
        relay._finish_client_cancel_cleanup(
            _Manager(),
            session_id,
            on_interrupt_confirmed=on_interrupt_confirmed,
        )
    ) is False

    assert session_id not in SESSION_INFOS
    assert SESSION_TOOL_CALLS[session_id][0]["status"] == "running"
    assert broadcasts == []
    assert callback_calls == 0
    assert "private prompt token" not in caplog.text
    assert "clarify response" not in caplog.text
    SESSION_TOOL_CALLS.pop(session_id, None)


def test_client_disconnect_interrupts_once_and_closes_stream_without_sse_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "cancelled-stream-chat"
    SESSION_INFOS.pop(session_id, None)
    source_started = asyncio.Event()
    allow_interrupt = asyncio.Event()
    interrupt_started = asyncio.Event()
    prompt_calls = 0
    interrupt_calls = 0
    source_closes = 0
    status_updates: list[dict[str, Any]] = []
    broadcasts: list[dict[str, Any]] = []
    finalized_stream_ids: list[set[str]] = []
    cleanup_order: list[str] = []

    class _Manager:
        is_connected = True

        def has_prompt_history(self, _session_id: str) -> bool:
            return False

        async def submit_prompt(self, *_args: Any, **_kwargs: Any):
            nonlocal prompt_calls, source_closes
            prompt_calls += 1
            try:
                yield {"type": "tool.start", "tool": {"tool_id": "tool-a", "name": "clarify", "args_text": "private-a"}}
                yield {"type": "tool.start", "tool": {"tool_id": "tool-b", "name": "exec", "args_text": "private-b"}}
                source_started.set()
                await asyncio.Event().wait()
                yield {"type": "text", "text": "must not be delivered"}
            finally:
                source_closes += 1

        async def interrupt_session(self, _session_id: str) -> dict[str, str]:
            nonlocal interrupt_calls
            interrupt_calls += 1
            interrupt_started.set()
            await allow_interrupt.wait()
            return {"status": "interrupted"}

    def fake_update(_session_id: str, status: dict[str, Any]) -> dict[str, Any]:
        status_updates.append(status)
        cleanup_order.append("agent-status")
        return status

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    async def fake_finalize(_session_id: str, stream_ids: set[str]) -> None:
        finalized_stream_ids.append(set(stream_ids))
        cleanup_order.append("tool-calls")

    monkeypatch.setattr(relay, "workspace_cwd_for_selection", lambda _selection: None)
    monkeypatch.setattr(relay, "update_agent_status", fake_update)
    monkeypatch.setattr(relay, "ws_broadcast", fake_broadcast)
    monkeypatch.setattr(relay, "save_tool_calls_from_output", lambda *_args: None)
    monkeypatch.setattr(relay, "finalize_running_tool_calls_for_stream", fake_finalize)

    async def run() -> None:
        response = await relay.forward_hermes_streaming(
            {"messages": [{"role": "user", "content": "hello"}]},
            session_id,
            _Manager(),
        )
        iterator = response.body_iterator
        first_chunk = await anext(iterator)
        assert '"role": "assistant"' in first_chunk
        consumer = asyncio.create_task(anext(iterator))
        await source_started.wait()
        consumer.cancel()
        await interrupt_started.wait()
        assert not consumer.done()
        allow_interrupt.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await asyncio.sleep(0)
        assert prompt_calls == 1
        assert interrupt_calls == 1
        assert source_closes == 1
        assert finalized_stream_ids == [{"tool-a", "tool-b"}]
        assert cleanup_order[-2:] == ["tool-calls", "agent-status"]
        assert status_updates[-1] == {"active": False}
        assert broadcasts[-1]["status"] == {"active": False}

    asyncio.run(run())
    SESSION_INFOS.pop(session_id, None)


def _relay_manager_with_session(monkeypatch: Any) -> tuple[HermesWebSocketManager, HermesSession]:
    manager = HermesWebSocketManager()
    session = HermesSession(tui_session_id="tui-1", st_session_id="chat-1")
    manager._sessions["tui-1"] = session
    manager._st_to_tui["chat-1"] = "tui-1"
    manager._should_connect = True
    manager._ready = True
    manager._ws = type("OpenWebSocket", (), {"open": True})()

    async def ensure_session(*_args: Any, **_kwargs: Any) -> str:
        return "tui-1"

    monkeypatch.setattr(manager, "ensure_session", ensure_session)
    monkeypatch.setattr(manager, "_persist_session_info", lambda _session: None)
    monkeypatch.setattr(relay, "workspace_cwd_for_selection", lambda _selection: None)
    monkeypatch.setattr(relay, "update_agent_status", lambda _session_id, status: status or {"active": False})

    async def no_broadcast(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(relay, "ws_broadcast", no_broadcast)
    return manager, session


def test_json_rpc_error_through_relay_closes_prompt_generator_immediately(monkeypatch: Any) -> None:
    manager, session = _relay_manager_with_session(monkeypatch)

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        await manager._handle_response({
            "id": request_id,
            "error": {"code": 4090, "message": "session is busy"},
        })

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def consume() -> list[str]:
        response = await relay.forward_hermes_streaming(
            {"messages": [{"role": "user", "content": "hello"}]},
            "chat-1",
            manager,
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())

    assert "session is busy" in "".join(chunks)
    assert "invalid acknowledgement" not in "".join(chunks)
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False
    assert session.turn_complete_event.is_set()


def test_accepted_hermes_submission_is_explicit_on_frontend_stream(monkeypatch: Any) -> None:
    manager, session = _relay_manager_with_session(monkeypatch)

    async def fake_send(request_id: int, method: str, _params: dict[str, Any]) -> None:
        assert method == "prompt.submit"
        await manager._handle_response({"id": request_id, "result": {"status": "queued"}})

    monkeypatch.setattr(manager, "_send_json_rpc", fake_send)

    async def consume() -> list[str]:
        response = await relay.forward_hermes_streaming(
            {"messages": [{"role": "user", "content": "hello"}]},
            "chat-1",
            manager,
        )
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume())
    acceptance = json.loads(chunks[1][len("data: ") :])

    assert acceptance["choices"][0]["delta"]["st_proxy"] == {
        "submission": "accepted",
        "accepted": True,
        "retry": False,
        "status": "queued",
        "message": "Hermes accepted the prompt as queued; no dedicated stream is attached. Do not retry.",
    }
    assert "invalid acknowledgement" not in "".join(chunks)
    assert '"finish_reason": "stop"' in "".join(chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
    assert session.pending_queues == {}
    assert manager._rpc_waiters == {}
    assert session.current_request_id is None
    assert session.is_processing is False
    assert session.turn_complete_event.is_set()
