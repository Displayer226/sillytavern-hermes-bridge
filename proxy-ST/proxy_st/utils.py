import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from urllib.parse import parse_qsl, urlencode

from .config import LOG_BODY_MAX_CHARS, SENSITIVE_HEADERS


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truncate(value: str) -> str:
    if LOG_BODY_MAX_CHARS <= 0 or len(value) <= LOG_BODY_MAX_CHARS:
        return value
    return value[:LOG_BODY_MAX_CHARS] + f"...<truncated {len(value) - LOG_BODY_MAX_CHARS} chars>"


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        sanitized[lower] = "***redacted***" if lower in SENSITIVE_HEADERS else value
    return sanitized


def sanitize_query(query: str) -> str:
    if not query:
        return ""
    sensitive = {"token", "access_token", "api_key", "key"}
    pairs = parse_qsl(query, keep_blank_values=True)
    sanitized = [
        (key, "***redacted***" if key.lower() in sensitive else value)
        for key, value in pairs
    ]
    return urlencode(sanitized, doseq=True)


def has_unresolved_macro(value: str | None) -> bool:
    return bool(value and ("{{" in value or "}}" in value))


def usable_metadata_value(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or has_unresolved_macro(text):
        return None
    return text


def stable_body_fingerprint(body: Any) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def tool_output_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("output")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content") or value.get("output")
        if isinstance(text, str) and text:
            return text
        stream_parts: list[str] = []
        for key in ("stdout", "stderr"):
            stream_value = value.get(key)
            if isinstance(stream_value, str) and stream_value:
                stream_parts.append(stream_value)
        if stream_parts:
            return "\n".join(stream_parts)
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def first_tool_text(*values: Any) -> str | None:
    for value in values:
        text = tool_output_text(value)
        if text and text.strip():
            return text
    return None
