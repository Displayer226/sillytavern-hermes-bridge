import asyncio
import json
from typing import Any

from .log import logger
from .persistence import _save_sessions
from .realtime import ws_broadcast
from .responses import responses_usage_to_chat_usage
from .state import SESSION_INFOS, SESSION_TOOL_CALLS
from .utils import now_iso, tool_output_text

INTERRUPTED_TOOL_CALL_ERROR = "Interrupted because the client disconnected."
_INTERRUPTED_TOOL_CALL_BROADCAST_TIMEOUT_SECONDS = 1.0


def _silence_task_exception(task: asyncio.Task) -> None:
    """Prevent 'Future exception was never retrieved' warnings for fire-and-forget broadcasts."""
    try:
        task.result()
    except Exception:
        logger.debug("Background broadcast task failed", exc_info=True)


def normalize_session_usage(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None

    if any(key in usage for key in ("context_max", "context_used", "input", "output")):
        normalized = dict(usage)
        context_max = int(normalized.get("context_max") or 0)
        context_used = int(normalized.get("context_used") or 0)
        if context_max and context_used:
            normalized["context_remaining"] = max(0, context_max - context_used)
            normalized["context_percent"] = max(0, min(100, round(context_used / context_max * 100)))
        # Pass through all Hermes fields for enhanced context bar
        for field in ("reasoning", "cache_read", "cache_write", "cost_usd", "cost_status", "compressions", "model", "calls"):
            if field in usage and field not in normalized:
                normalized[field] = usage[field]
        return normalized

    return responses_usage_to_chat_usage(usage)


def _is_nonzero_exit_code(value: Any) -> bool:
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _todos_from_value(value: Any, *, allow_list: bool = False) -> list[Any] | None:
    decoded = _decode_json_value(value)
    if isinstance(decoded, dict) and isinstance(decoded.get("todos"), list):
        return decoded["todos"]
    if allow_list and isinstance(decoded, list):
        return decoded
    return None


def _looks_like_todo_call(item: dict[str, Any], existing: dict[str, Any] | None = None) -> bool:
    names = (item.get("name"), existing.get("name") if isinstance(existing, dict) else None)
    return any("todo" in str(name or "").lower() for name in names)


def _extract_todos(item: dict[str, Any], existing: dict[str, Any] | None = None) -> list[Any] | None:
    direct_todos = _todos_from_value(item.get("todos"), allow_list=True)
    if direct_todos is not None:
        return direct_todos

    is_todo_call = _looks_like_todo_call(item, existing)
    for key in ("output", "result_text"):
        todos = _todos_from_value(item.get(key), allow_list=is_todo_call)
        if todos is not None:
            return todos

    if is_todo_call:
        return _todos_from_value(item.get("arguments"), allow_list=False)
    return None


def _copy_tool_call_fields(target: dict[str, Any], item: dict[str, Any]) -> None:
    for key in ("duration_s", "error", "todos", "inline_diff", "result_text", "stdout", "stderr", "exit_code", "returncode"):
        if item.get(key) is not None:
            target[key] = item.get(key)


def _copy_todos_snapshot(target: dict[str, Any], item: dict[str, Any]) -> None:
    todos = _extract_todos(item, target)
    if todos is not None:
        target["todos"] = todos


def save_tool_calls_from_output(session_id: str, output_list: list[Any]) -> None:
    if not session_id or not isinstance(output_list, list):
        return
    if session_id not in SESSION_TOOL_CALLS:
        SESSION_TOOL_CALLS[session_id] = []

    events: list[dict[str, Any]] = []

    for item in output_list:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id")
            if call_id:
                call_id = str(call_id)
                exit_code = item.get("exit_code", item.get("returncode"))
                is_error = bool(item.get("error")) or _is_nonzero_exit_code(exit_code)
                normalized_status = "error" if is_error else ("completed" if item.get("status") == "completed" else "running")
                existing = next((x for x in SESSION_TOOL_CALLS[session_id] if x.get("id") == call_id), None)
                if not existing:
                    new_call = {
                        "id": call_id,
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                        "status": normalized_status,
                        "output": None,
                        "timestamp": now_iso(),
                    }
                    text_val = tool_output_text(item.get("output"))
                    if text_val:
                        new_call["output"] = text_val
                    _copy_tool_call_fields(new_call, item)
                    _copy_todos_snapshot(new_call, item)
                    SESSION_TOOL_CALLS[session_id].append(new_call)
                    events.append({"event": "tool_call_added", "tool_call": new_call})
                elif item.get("status") == "completed" or is_error:
                    existing["status"] = normalized_status
                    text_val = tool_output_text(item.get("output"))
                    if text_val:
                        existing["output"] = text_val
                    _copy_tool_call_fields(existing, item)
                    _copy_todos_snapshot(existing, item)
                    events.append({"event": "tool_call_updated", "tool_call": existing})
        elif item_type == "function_call_output":
            call_id = item.get("call_id") or item.get("id")
            if call_id:
                call_id = str(call_id)
                exit_code = item.get("exit_code", item.get("returncode"))
                is_error = bool(item.get("error")) or _is_nonzero_exit_code(exit_code)
                existing = next((x for x in SESSION_TOOL_CALLS[session_id] if x.get("id") == call_id), None)
                if not existing:
                    existing = {
                        "id": call_id,
                        "name": item.get("name") or call_id,
                        "arguments": None,
                        "status": "error" if is_error else "completed",
                        "output": None,
                        "timestamp": now_iso(),
                    }
                    SESSION_TOOL_CALLS[session_id].append(existing)
                    events.append({"event": "tool_call_added", "tool_call": existing})
                elif item.get("name") and (not existing.get("name") or existing.get("name") == call_id):
                    existing["name"] = item.get("name")
                text_val = tool_output_text(item.get("output"))
                if text_val:
                    existing["output"] = text_val
                _copy_tool_call_fields(existing, item)
                _copy_todos_snapshot(existing, item)
                existing["status"] = "error" if is_error else "completed"
                events.append({"event": "tool_call_completed", "tool_call": existing})

    for evt in events:
        evt["type"] = evt.pop("event")
        evt["session_id"] = session_id
        task = asyncio.create_task(ws_broadcast(session_id, evt))
        task.add_done_callback(_silence_task_exception)

    if events:
        _save_sessions()


async def finalize_running_tool_calls_for_stream(session_id: str, tool_call_ids: set[str]) -> int:
    """Mark only this stream's still-running calls as client-disconnect errors."""
    if not session_id or not tool_call_ids:
        return 0

    calls = SESSION_TOOL_CALLS.get(session_id, [])
    terminal_events: list[dict[str, Any]] = []
    for call in calls:
        call_id = str(call.get("id") or "")
        if call_id not in tool_call_ids or call.get("status") != "running":
            continue
        call["status"] = "error"
        call["error"] = INTERRUPTED_TOOL_CALL_ERROR
        terminal_events.append(
            {
                "type": "tool_call_completed",
                "session_id": session_id,
                "tool_call": {
                    "id": call_id,
                    "status": "error",
                    "error": INTERRUPTED_TOOL_CALL_ERROR,
                },
            }
        )

    if not terminal_events:
        return 0

    _save_sessions()
    broadcast_tasks = [
        asyncio.create_task(ws_broadcast(session_id, event))
        for event in terminal_events
    ]
    timed_out = False
    try:
        _, pending = await asyncio.wait(
            broadcast_tasks,
            timeout=_INTERRUPTED_TOOL_CALL_BROADCAST_TIMEOUT_SECONDS,
        )
        if pending:
            timed_out = True
            for task in pending:
                task.cancel()
    except asyncio.CancelledError:
        for task in broadcast_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*broadcast_tasks, return_exceptions=True)
        raise

    results = await asyncio.gather(*broadcast_tasks, return_exceptions=True)
    if timed_out:
        logger.warning("Interrupted tool call broadcasts timed out")
    for result in results:
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.warning(
                "Interrupted tool call broadcast failed; error_type=%s",
                type(result).__name__,
            )
    return len(terminal_events)


async def clear_session_tool_calls(session_id: str, *, reason: str = "manual") -> int:
    """Clear every tool-derived panel for a session and notify its clients."""
    calls = SESSION_TOOL_CALLS.get(session_id, [])
    removed = len(calls)
    SESSION_TOOL_CALLS[session_id] = []
    _save_sessions()
    await ws_broadcast(session_id, {
        "type": "tool_calls_cleared",
        "session_id": session_id,
        "reason": reason,
        "removed": removed,
    })
    return removed


def filter_tool_calls(
    calls: list[dict[str, Any]],
    *,
    status: str | None = None,
    tool: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    normalized_limit = max(1, min(int(limit), 200))
    normalized_offset = max(0, int(offset))
    status_filter = status.strip().lower() if isinstance(status, str) and status.strip() else None
    tool_filter = tool.strip().lower() if isinstance(tool, str) and tool.strip() else None
    query = q.strip().lower() if isinstance(q, str) and q.strip() else None

    filtered = []
    for call in calls:
        if status_filter and str(call.get("status") or "").lower() != status_filter:
            continue
        if tool_filter and tool_filter not in str(call.get("name") or "").lower():
            continue
        if query:
            haystack = " ".join(
                str(call.get(key) or "")
                for key in ("name", "arguments", "output", "result_text", "stdout", "stderr", "error")
            ).lower()
            if query not in haystack:
                continue
        filtered.append(call)

    ordered = list(reversed(filtered))
    total = len(ordered)
    page = ordered[normalized_offset:normalized_offset + normalized_limit]
    return {
        "tool_calls": page,
        "total": total,
        "limit": normalized_limit,
        "offset": normalized_offset,
        "has_more": normalized_offset + normalized_limit < total,
    }


def _update_session_info(session_id: str, *, usage: Any = None) -> None:
    if not session_id:
        return
    info = SESSION_INFOS.setdefault(session_id, {"last_usage": None, "total_requests": 0})
    info["total_requests"] = info.get("total_requests", 0) + 1
    normalized_usage = normalize_session_usage(usage)
    if normalized_usage is not None:
        info["last_usage"] = normalized_usage
    info["updated_at"] = now_iso()
    _save_sessions()


def update_agent_status(session_id: str, status: dict[str, Any] | None) -> dict[str, Any]:
    info = SESSION_INFOS.setdefault(session_id, {"last_usage": None, "total_requests": 0})
    info["agent_status"] = status or {"active": False}
    info["updated_at"] = now_iso()
    if not info["agent_status"].get("active"):
        _save_sessions()
    return info["agent_status"]


def merge_live_session_info(session_id: str, live_info: dict[str, Any] | None) -> dict[str, Any]:
    info = SESSION_INFOS.setdefault(session_id, {"last_usage": None, "total_requests": 0})
    if live_info:
        info["hermes"] = live_info
        usage = normalize_session_usage(live_info.get("usage"))
        if usage is not None:
            info["last_usage"] = usage
        if live_info.get("model"):
            info["model"] = live_info.get("model")
        if live_info.get("reasoning_effort") is not None:
            info["reasoning_effort"] = live_info.get("reasoning_effort")
        info["updated_at"] = now_iso()
        _save_sessions()
    return info
