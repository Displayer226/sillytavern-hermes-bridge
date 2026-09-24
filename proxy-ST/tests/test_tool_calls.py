from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from proxy_st import tool_calls
from proxy_st.state import SESSION_INFOS, SESSION_TOOL_CALLS


class _DoneTask:
    def add_done_callback(self, callback: Any) -> None:
        callback(self)

    def result(self) -> None:
        return None


def _fake_create_task(coro: Any) -> _DoneTask:
    coro.close()
    return _DoneTask()


@pytest.fixture(autouse=True)
def reset_global_state():
    SESSION_TOOL_CALLS.clear()
    SESSION_INFOS.clear()
    try:
        yield
    finally:
        SESSION_TOOL_CALLS.clear()
        SESSION_INFOS.clear()


def test_save_tool_calls_normalizes_and_merges(monkeypatch: Any) -> None:
    monkeypatch.setattr(tool_calls.asyncio, "create_task", _fake_create_task)
    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: None)

    tool_calls.save_tool_calls_from_output(
        "chat-1",
        [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "exec_command",
                "arguments": '{"cmd":"false"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "failed",
                "returncode": "1",
                "error": "exit 1",
            },
        ],
    )

    calls = SESSION_TOOL_CALLS["chat-1"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call-1"
    assert calls[0]["name"] == "exec_command"
    assert calls[0]["status"] == "error"
    assert calls[0]["output"] == "failed"
    assert calls[0]["returncode"] == "1"


def test_save_tool_calls_extracts_todos_from_output_json(monkeypatch: Any) -> None:
    monkeypatch.setattr(tool_calls.asyncio, "create_task", _fake_create_task)
    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: None)

    todos = [{"id": "task-1", "content": "Write tests", "status": "completed"}]
    tool_calls.save_tool_calls_from_output(
        "chat-1",
        [
            {
                "type": "function_call",
                "call_id": "todo-call",
                "name": "todo",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "todo-call",
                "output": json.dumps({"todos": todos, "summary": {"completed": 1, "total": 1}}),
            },
        ],
    )

    calls = SESSION_TOOL_CALLS["chat-1"]
    assert len(calls) == 1
    assert calls[0]["todos"] == todos


def test_save_tool_calls_extracts_empty_todo_snapshot(monkeypatch: Any) -> None:
    monkeypatch.setattr(tool_calls.asyncio, "create_task", _fake_create_task)
    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: None)

    tool_calls.save_tool_calls_from_output(
        "chat-1",
        [
            {
                "type": "function_call_output",
                "call_id": "todo-read",
                "name": "todo",
                "output": json.dumps({"todos": [], "summary": {"completed": 0, "total": 0}}),
            },
        ],
    )

    calls = SESSION_TOOL_CALLS["chat-1"]
    assert len(calls) == 1
    assert calls[0]["todos"] == []


def test_finalize_running_tool_calls_for_stream_is_scoped_and_redacted(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    SESSION_TOOL_CALLS["chat-1"] = [
        {
            "id": "current-running",
            "name": "clarify",
            "arguments": "private arguments",
            "output": "private partial result",
            "status": "running",
        },
        {"id": "current-completed", "status": "completed", "output": "done"},
        {"id": "current-error", "status": "error", "error": "old error"},
        {"id": "old-running", "status": "running", "output": "old partial result"},
    ]
    saved: list[bool] = []
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: saved.append(True))
    monkeypatch.setattr(tool_calls, "ws_broadcast", fake_broadcast)

    changed = asyncio.run(
        tool_calls.finalize_running_tool_calls_for_stream(
            "chat-1",
            {"current-running", "current-completed", "current-error"},
        )
    )

    assert changed == 1
    assert saved == [True]
    assert SESSION_TOOL_CALLS["chat-1"][0]["status"] == "error"
    assert SESSION_TOOL_CALLS["chat-1"][0]["error"] == tool_calls.INTERRUPTED_TOOL_CALL_ERROR
    assert SESSION_TOOL_CALLS["chat-1"][1] == {"id": "current-completed", "status": "completed", "output": "done"}
    assert SESSION_TOOL_CALLS["chat-1"][2] == {"id": "current-error", "status": "error", "error": "old error"}
    assert SESSION_TOOL_CALLS["chat-1"][3]["status"] == "running"
    assert broadcasts == [
        {
            "type": "tool_call_completed",
            "session_id": "chat-1",
            "tool_call": {
                "id": "current-running",
                "status": "error",
                "error": tool_calls.INTERRUPTED_TOOL_CALL_ERROR,
            },
        }
    ]
    assert "private arguments" not in json.dumps(broadcasts)
    assert "private partial result" not in json.dumps(broadcasts)
    assert "private arguments" not in caplog.text
    assert "private partial result" not in caplog.text


def test_finalize_running_tool_calls_for_stream_is_idempotent_for_multiple_calls(
    monkeypatch: Any,
) -> None:
    SESSION_TOOL_CALLS["chat-1"] = [
        {"id": "call-a", "status": "running"},
        {"id": "call-b", "status": "running"},
    ]
    saved: list[bool] = []
    broadcasts: list[dict[str, Any]] = []

    async def fake_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append(message)

    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: saved.append(True))
    monkeypatch.setattr(tool_calls, "ws_broadcast", fake_broadcast)

    async def run() -> tuple[int, int]:
        first = await tool_calls.finalize_running_tool_calls_for_stream("chat-1", {"call-a", "call-b"})
        second = await tool_calls.finalize_running_tool_calls_for_stream("chat-1", {"call-a", "call-b"})
        return first, second

    assert asyncio.run(run()) == (2, 0)
    assert saved == [True]
    assert [event["tool_call"]["id"] for event in broadcasts] == ["call-a", "call-b"]


def test_finalize_running_tool_calls_for_stream_has_one_global_broadcast_timeout(
    monkeypatch: Any,
) -> None:
    call_ids = {"call-a", "call-b", "call-c"}
    SESSION_TOOL_CALLS["chat-1"] = [{"id": call_id, "status": "running"} for call_id in call_ids]
    saved: list[bool] = []
    started: set[str] = set()
    cancelled: set[str] = set()

    async def blocked_broadcast(_session_id: str, message: dict[str, Any]) -> None:
        call_id = message["tool_call"]["id"]
        started.add(call_id)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(call_id)
            raise

    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: saved.append(True))
    monkeypatch.setattr(tool_calls, "ws_broadcast", blocked_broadcast)
    monkeypatch.setattr(tool_calls, "_INTERRUPTED_TOOL_CALL_BROADCAST_TIMEOUT_SECONDS", 0.01)

    async def run() -> int:
        started_at = time.monotonic()
        changed = await tool_calls.finalize_running_tool_calls_for_stream("chat-1", call_ids)
        assert time.monotonic() - started_at < 0.2
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
        return changed

    assert asyncio.run(run()) == len(call_ids)
    assert started == call_ids
    assert cancelled == call_ids
    assert saved == [True]
    assert {call["status"] for call in SESSION_TOOL_CALLS["chat-1"]} == {"error"}


def test_filter_tool_calls_paginates_recent_first() -> None:
    calls = [
        {"id": "1", "name": "read_file", "status": "completed", "arguments": "alpha"},
        {"id": "2", "name": "exec_command", "status": "error", "stderr": "boom"},
        {"id": "3", "name": "exec_command", "status": "completed", "output": "done"},
    ]

    page = tool_calls.filter_tool_calls(calls, limit=1, offset=0, tool="exec")
    assert page["total"] == 2
    assert page["has_more"] is True
    assert [call["id"] for call in page["tool_calls"]] == ["3"]

    error_page = tool_calls.filter_tool_calls(calls, status="error", q="boom")
    assert error_page["total"] == 1
    assert error_page["tool_calls"][0]["id"] == "2"


def test_clear_session_tool_calls_persists_and_broadcasts(monkeypatch: Any) -> None:
    SESSION_TOOL_CALLS["chat-1"] = [{"id": "1"}, {"id": "2"}]
    saved: list[bool] = []
    broadcasts: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(tool_calls, "_save_sessions", lambda: saved.append(True))

    async def fake_broadcast(session_id: str, message: dict[str, Any]) -> None:
        broadcasts.append((session_id, message))

    monkeypatch.setattr(tool_calls, "ws_broadcast", fake_broadcast)

    removed = __import__("asyncio").run(
        tool_calls.clear_session_tool_calls("chat-1", reason="undo")
    )

    assert removed == 2
    assert SESSION_TOOL_CALLS["chat-1"] == []
    assert saved == [True]
    assert broadcasts == [
        (
            "chat-1",
            {
                "type": "tool_calls_cleared",
                "session_id": "chat-1",
                "reason": "undo",
                "removed": 2,
            },
        )
    ]
