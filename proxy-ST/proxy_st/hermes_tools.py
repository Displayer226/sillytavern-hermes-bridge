import uuid
from typing import Any

from .utils import first_tool_text, now_iso, tool_output_text


def hermes_tool_call_id(tool: dict[str, Any]) -> str:
    for key in ("tool_id", "call_id", "id"):
        value = tool.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return f"call_{uuid.uuid4().hex[:8]}"


def hermes_tool_start_item(tool: dict[str, Any]) -> dict[str, Any]:
    call_id = hermes_tool_call_id(tool)
    return {
        "type": "function_call",
        "id": call_id,
        "call_id": call_id,
        "name": tool.get("name") or "unknown",
        "arguments": first_tool_text(tool.get("args_text"), tool.get("context"), tool.get("arguments")) or "",
        "status": "running",
        "timestamp": now_iso(),
    }


def hermes_tool_complete_item(tool: dict[str, Any], started_call: dict[str, Any] | None = None) -> dict[str, Any]:
    call_id = hermes_tool_call_id(tool)
    stream_output = "\n".join(
        part
        for part in (tool_output_text(tool.get("stdout")), tool_output_text(tool.get("stderr")))
        if part
    )
    output = first_tool_text(
        tool.get("result_text"),
        stream_output,
        tool.get("summary"),
        tool.get("inline_diff"),
        tool.get("error"),
        tool.get("todos"),
    )
    item = {
        "type": "function_call_output",
        "id": f"{call_id}_output",
        "call_id": call_id,
        "name": tool.get("name") or (started_call or {}).get("name") or "unknown",
        "status": "completed",
        "output": output or "",
        "timestamp": now_iso(),
    }
    for key in ("duration_s", "error", "todos", "inline_diff", "result_text", "stdout", "stderr", "exit_code", "returncode"):
        if tool.get(key) is not None:
            item[key] = tool.get(key)
    return item
