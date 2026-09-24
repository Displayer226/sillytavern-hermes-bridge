from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from proxy_st import persistence
from proxy_st.state import SESSION_INFOS, SESSION_TOOL_CALLS


def setup_function() -> None:
    SESSION_TOOL_CALLS.clear()
    SESSION_INFOS.clear()
    persistence._close_sqlite_conn()


def teardown_function() -> None:
    SESSION_TOOL_CALLS.clear()
    SESSION_INFOS.clear()
    persistence._close_sqlite_conn()
    persistence._pending_payload = None
    persistence._persist_writer_task = None


def _sample_payload() -> dict[str, Any]:
    return {
        "tool_calls": {
            "chat-1": [
                {"id": "call-1", "name": "exec_command", "status": "completed"},
            ],
        },
        "infos": {
            "chat-1": {
                "total_requests": 2,
                "agent_status": {"active": True},
                "hermes": {
                    "pending_clarify": {"request_id": "x"},
                    "pending_approvals": [{"command": "rm"}],
                    "pending_sudo": {"request_id": "sudo-1"},
                },
            },
        },
        "saved_at": "2026-06-06T00:00:00Z",
    }


def test_json_persistence_round_trip(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(persistence, "SESSIONS_DATA_FILE", tmp_path / "sessions.json")

    persistence._save_sessions_json(_sample_payload())
    assert persistence._load_sessions_json() is True

    assert SESSION_TOOL_CALLS["chat-1"][0]["id"] == "call-1"
    assert SESSION_INFOS["chat-1"]["total_requests"] == 2
    assert SESSION_INFOS["chat-1"]["agent_status"] == {"active": False}
    assert "pending_clarify" not in SESSION_INFOS["chat-1"]["hermes"]
    assert "pending_approvals" not in SESSION_INFOS["chat-1"]["hermes"]
    assert "pending_sudo" not in SESSION_INFOS["chat-1"]["hermes"]


def test_sqlite_persistence_round_trip(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(persistence, "SESSIONS_DB_FILE", tmp_path / "sessions.sqlite3")

    persistence._save_sessions_sqlite(_sample_payload())
    SESSION_TOOL_CALLS.clear()
    SESSION_INFOS.clear()

    assert persistence._load_sessions_sqlite() is True
    assert SESSION_TOOL_CALLS["chat-1"][0]["name"] == "exec_command"
    assert SESSION_INFOS["chat-1"]["total_requests"] == 2


@pytest.mark.asyncio
async def test_async_persistence_writer_serializes_latest_payload(monkeypatch: Any) -> None:
    monkeypatch.setattr(persistence, "SESSION_PERSISTENCE_ENABLED", True)
    writes: list[str] = []
    gate = asyncio.Event()

    async def fake_to_thread(func, payload):  # noqa: ANN001
        writes.append(payload["saved_at"])
        await gate.wait()

    monkeypatch.setattr(persistence.asyncio, "to_thread", fake_to_thread)

    persistence._save_sessions({"tool_calls": {}, "infos": {}, "saved_at": "old"})
    await asyncio.sleep(0)
    persistence._save_sessions({"tool_calls": {}, "infos": {}, "saved_at": "new"})

    assert writes == ["old"]
    gate.set()
    assert persistence._persist_writer_task is not None
    await persistence._persist_writer_task
    assert writes == ["old", "new"]
