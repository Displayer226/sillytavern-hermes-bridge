import asyncio
import json
import sqlite3
import tempfile
from threading import RLock
from typing import Any

from .config import (
    SESSION_PERSISTENCE_BACKEND,
    SESSION_PERSISTENCE_ENABLED,
    SESSION_PERSISTENCE_INTERVAL,
    SESSIONS_DATA_FILE,
    SESSIONS_DB_FILE,
)
from .log import logger
from .state import SESSION_INFOS, SESSION_TOOL_CALLS
from .utils import now_iso


_PERSIST_LOCK = RLock()
_SQLITE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        tool_calls TEXT NOT NULL DEFAULT '[]',
        info TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS session_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


def _build_persistable_data() -> dict[str, Any]:
    infos: dict[str, dict[str, Any]] = {}
    for session_id, info in SESSION_INFOS.items():
        safe_info = dict(info)
        hermes_info = safe_info.get("hermes")
        if isinstance(hermes_info, dict):
            hermes_info = dict(hermes_info)
            for key in ("pending_clarify", "pending_approvals", "pending_sudo"):
                hermes_info.pop(key, None)
            safe_info["hermes"] = hermes_info
        infos[session_id] = safe_info
    return {
        "tool_calls": dict(SESSION_TOOL_CALLS),
        "infos": infos,
        "saved_at": now_iso(),
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _sanitize_loaded_info(info: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(info)
    agent_status = sanitized.get("agent_status")
    if isinstance(agent_status, dict) and agent_status.get("active"):
        sanitized["agent_status"] = {"active": False}

    hermes_info = sanitized.get("hermes")
    if isinstance(hermes_info, dict):
        hermes_info = dict(hermes_info)
        for key in ("pending_clarify", "pending_approvals", "pending_sudo"):
            hermes_info.pop(key, None)
        sanitized["hermes"] = hermes_info
    return sanitized


def _apply_loaded_payload(data: dict[str, Any], source: str) -> None:
    tool_calls = data.get("tool_calls") if isinstance(data.get("tool_calls"), dict) else {}
    infos = data.get("infos") if isinstance(data.get("infos"), dict) else {}

    normalized_calls = {
        str(session_id): calls if isinstance(calls, list) else []
        for session_id, calls in tool_calls.items()
    }
    normalized_infos = {
        str(session_id): _sanitize_loaded_info(info if isinstance(info, dict) else {})
        for session_id, info in infos.items()
    }

    SESSION_TOOL_CALLS.clear()
    SESSION_TOOL_CALLS.update(normalized_calls)

    SESSION_INFOS.clear()
    SESSION_INFOS.update(normalized_infos)

    loaded_sessions = set(normalized_calls) | set(normalized_infos)
    logger.info("Loaded %d persisted session(s) from %s", len(loaded_sessions), source)


_persistent_sqlite_conn: sqlite3.Connection | None = None


def _get_sqlite_conn() -> sqlite3.Connection:
    """Return a persistent SQLite connection (thread-safe via check_same_thread=False)."""
    global _persistent_sqlite_conn
    if _persistent_sqlite_conn is not None:
        try:
            _persistent_sqlite_conn.execute("SELECT 1")
            return _persistent_sqlite_conn
        except Exception:
            # Connection is stale / broken — discard and reconnect.
            try:
                _persistent_sqlite_conn.close()
            except Exception:
                pass
            _persistent_sqlite_conn = None

    SESSIONS_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SESSIONS_DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for statement in _SQLITE_SCHEMA:
        conn.execute(statement)
    _persistent_sqlite_conn = conn
    return conn


def _close_sqlite_conn() -> None:
    global _persistent_sqlite_conn
    if _persistent_sqlite_conn is not None:
        try:
            _persistent_sqlite_conn.close()
        except Exception:
            pass
        _persistent_sqlite_conn = None


def _connect_sqlite() -> sqlite3.Connection:
    SESSIONS_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SESSIONS_DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for statement in _SQLITE_SCHEMA:
        conn.execute(statement)
    return conn


def _save_sessions_sqlite(payload: dict[str, Any]) -> None:
    tool_calls = payload.get("tool_calls") if isinstance(payload.get("tool_calls"), dict) else {}
    infos = payload.get("infos") if isinstance(payload.get("infos"), dict) else {}
    saved_at = str(payload.get("saved_at") or now_iso())
    session_ids = sorted(set(tool_calls) | set(infos))

    with _PERSIST_LOCK:
        conn = _get_sqlite_conn()
        rows = [
            (
                str(session_id),
                _json_dumps(tool_calls.get(session_id, [])),
                _json_dumps(infos.get(session_id, {})),
                str((infos.get(session_id) or {}).get("updated_at") or saved_at),
            )
            for session_id in session_ids
        ]
        conn.executemany(
            """
            INSERT INTO sessions (session_id, tool_calls, info, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                tool_calls = excluded.tool_calls,
                info = excluded.info,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            conn.execute(f"DELETE FROM sessions WHERE session_id NOT IN ({placeholders})", session_ids)
        else:
            conn.execute("DELETE FROM sessions")
        conn.execute(
            """
            INSERT INTO session_metadata (key, value)
            VALUES ('saved_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (saved_at,),
        )
        conn.commit()
    logger.debug("Sessions persisted to %s", SESSIONS_DB_FILE)


def _load_sessions_sqlite() -> bool:
    if not SESSIONS_DB_FILE.exists():
        logger.info("No persisted sessions database found at %s - starting fresh", SESSIONS_DB_FILE)
        return False

    with _PERSIST_LOCK:
        conn = _get_sqlite_conn()
        rows = conn.execute("SELECT session_id, tool_calls, info FROM sessions").fetchall()
        data = {"tool_calls": {}, "infos": {}}
        for session_id, tool_calls_json, info_json in rows:
            calls = _json_loads(tool_calls_json, [])
            info = _json_loads(info_json, {})
            data["tool_calls"][str(session_id)] = calls if isinstance(calls, list) else []
            data["infos"][str(session_id)] = info if isinstance(info, dict) else {}

    _apply_loaded_payload(data, str(SESSIONS_DB_FILE))
    return True


def _read_json_payload(path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("Failed to read sessions from %s", path)
        return None


def _save_sessions_json(payload: dict[str, Any]) -> None:
    with _PERSIST_LOCK:
        SESSIONS_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=SESSIONS_DATA_FILE.parent,
            delete=False,
        ) as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            tmp_name = f.name
        try:
            from pathlib import Path

            Path(tmp_name).replace(SESSIONS_DATA_FILE)
        except Exception:
            try:
                from pathlib import Path

                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise
    logger.debug("Sessions persisted to %s", SESSIONS_DATA_FILE)


def _load_sessions_json() -> bool:
    data = _read_json_payload(SESSIONS_DATA_FILE)
    if data is None:
        logger.info("No persisted sessions file found at %s - starting fresh", SESSIONS_DATA_FILE)
        return False

    saved_at = data.get("saved_at", "unknown")
    _apply_loaded_payload(data, f"{SESSIONS_DATA_FILE} (saved at {saved_at})")
    return True


def _save_sessions(data: dict[str, Any] | None = None) -> None:
    """Persist session data without blocking the event loop.

    When called from an async context the actual I/O is offloaded to a
    thread-pool via ``loop.run_in_executor``.  During startup / shutdown
    (no running loop) the write happens synchronously instead.
    """
    if not SESSION_PERSISTENCE_ENABLED:
        return

    payload = data if data is not None else _build_persistable_data()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop (startup / shutdown) — persist synchronously.
        _do_persist(payload)
        return
    _schedule_persist(payload)


def _do_persist(payload: dict[str, Any]) -> None:
    """Perform the actual I/O write (runs in a thread when called from async)."""
    try:
        if SESSION_PERSISTENCE_BACKEND == "json":
            _save_sessions_json(payload)
        else:
            _save_sessions_sqlite(payload)
    except Exception:
        target = SESSIONS_DATA_FILE if SESSION_PERSISTENCE_BACKEND == "json" else SESSIONS_DB_FILE
        logger.exception("Failed to persist sessions to %s", target)


_persist_writer_task: asyncio.Task | None = None
_pending_payload: dict[str, Any] | None = None


def _schedule_persist(payload: dict[str, Any]) -> None:
    global _pending_payload, _persist_writer_task
    _pending_payload = payload
    if _persist_writer_task is None or _persist_writer_task.done():
        _persist_writer_task = asyncio.create_task(_persist_writer())
        _persist_writer_task.add_done_callback(_persist_done_callback)


async def _persist_writer() -> None:
    global _pending_payload
    while _pending_payload is not None:
        payload = _pending_payload
        _pending_payload = None
        await asyncio.to_thread(_do_persist, payload)


def _persist_done_callback(task: asyncio.Future) -> None:
    """Log exceptions from background persist tasks."""
    try:
        task.result()
    except Exception:
        logger.debug("Background session persist failed", exc_info=True)


def _load_sessions() -> None:
    if not SESSION_PERSISTENCE_ENABLED:
        return

    if SESSION_PERSISTENCE_BACKEND == "json":
        _load_sessions_json()
        return

    try:
        if _load_sessions_sqlite():
            return

        legacy_payload = _read_json_payload(SESSIONS_DATA_FILE)
        if legacy_payload:
            _apply_loaded_payload(legacy_payload, f"{SESSIONS_DATA_FILE} (legacy JSON)")
            _save_sessions_sqlite(_build_persistable_data())
    except Exception:
        logger.exception("Failed to load sessions from %s", SESSIONS_DB_FILE)


_persist_task: asyncio.Task | None = None


async def _periodic_persist() -> None:
    while True:
        await asyncio.sleep(SESSION_PERSISTENCE_INTERVAL)
        _save_sessions()


async def _startup_persist_loop() -> None:
    global _persist_task
    if SESSION_PERSISTENCE_ENABLED and SESSION_PERSISTENCE_INTERVAL > 0:
        _persist_task = asyncio.create_task(_periodic_persist())
        logger.info(
            "Periodic session persistence started (backend=%s interval=%ds)",
            SESSION_PERSISTENCE_BACKEND,
            SESSION_PERSISTENCE_INTERVAL,
        )


async def _shutdown_persist() -> None:
    global _pending_payload, _persist_task, _persist_writer_task
    if _persist_task:
        _persist_task.cancel()
        try:
            await _persist_task
        except asyncio.CancelledError:
            pass
        _persist_task = None
    if SESSION_PERSISTENCE_ENABLED:
        if _persist_writer_task and not _persist_writer_task.done():
            await _persist_writer_task
        _pending_payload = None
        await asyncio.to_thread(_do_persist, _build_persistable_data())
        _persist_writer_task = None
        _close_sqlite_conn()
        logger.info("Final session data persisted on shutdown")
