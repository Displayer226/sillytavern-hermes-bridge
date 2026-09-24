import json
import logging
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any

from fastapi import Request

from .config import LOG_DIR, LOG_INCLUDE_BODIES, LOG_REQUEST_BACKUP_COUNT, LOG_REQUEST_MAX_BYTES
from .log import logger, set_request_id
from .request_transform import (
    detect_backend,
    detect_session,
    extract_proxy_metadata,
    extract_session_marker,
    request_messages,
)
from .utils import has_unresolved_macro, now_iso, sanitize_headers, sanitize_query, stable_body_fingerprint, truncate


_request_jsonl_logger: logging.Logger | None = None


def _get_request_jsonl_logger() -> logging.Logger:
    global _request_jsonl_logger
    if _request_jsonl_logger is not None:
        return _request_jsonl_logger

    request_logger = logging.getLogger("proxy_st.requests_jsonl")
    request_logger.propagate = False
    request_logger.setLevel(logging.INFO)
    request_logger.handlers.clear()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "requests.jsonl",
        maxBytes=LOG_REQUEST_MAX_BYTES,
        backupCount=LOG_REQUEST_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(handler)
    _request_jsonl_logger = request_logger
    return request_logger


def request_summary(headers: dict[str, str], body: Any) -> dict[str, Any]:
    model = body.get("model") if isinstance(body, dict) else None
    messages = request_messages(body)
    session_id, session_source = detect_session(headers, body)
    marker = extract_session_marker(body)

    unresolved_macros = []
    for key, value in headers.items():
        if has_unresolved_macro(value):
            unresolved_macros.append(key)

    return {
        "session_id": session_id,
        "session_source": session_source,
        "backend": detect_backend(model, headers, body),
        "model": model,
        "stream": body.get("stream") if isinstance(body, dict) else None,
        "message_count": len(messages),
        "user": None if has_unresolved_macro(headers.get("x-st-user")) else headers.get("x-st-user"),
        "character": None
        if has_unresolved_macro(headers.get("x-st-character") or headers.get("x-st-char"))
        else headers.get("x-st-character") or headers.get("x-st-char"),
        "proxy_metadata": extract_proxy_metadata(body),
        "body_marker": marker,
        "unresolved_macro_headers": unresolved_macros,
    }


def append_jsonl(entry: dict[str, Any]) -> None:
    try:
        _get_request_jsonl_logger().info(json.dumps(entry, ensure_ascii=False, default=str))
    except OSError as exc:
        logger.warning("request JSONL log write skipped: %s", exc)


async def log_request(request: Request, body: Any, raw_body: str) -> dict[str, Any]:
    headers = sanitize_headers(dict(request.headers))
    summary = request_summary(headers, body if isinstance(body, dict) else {})
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    if not getattr(request.state, "request_id", None):
        request.state.request_id = request_id
        set_request_id(request_id)
    entry = {
        "timestamp": now_iso(),
        "request_id": request_id,
        "method": request.method,
        "path": request.url.path,
        "query": sanitize_query(str(request.url.query)),
        "client": request.client.host if request.client else None,
        "summary": summary,
        "headers": headers,
        "body_fingerprint": stable_body_fingerprint(body),
    }
    if LOG_INCLUDE_BODIES:
        entry["body"] = truncate(json.dumps(body, ensure_ascii=False, default=str))
        entry["raw_body"] = truncate(raw_body)

    append_jsonl(entry)
    logger.info("request %s %s", entry["request_id"], json.dumps(summary, ensure_ascii=False))
    logger.debug("headers %s", json.dumps(headers, ensure_ascii=False))
    logger.debug("body %s", truncate(json.dumps(body, ensure_ascii=False, indent=2, default=str)))

    if summary["unresolved_macro_headers"]:
        logger.warning(
            "unresolved SillyTavern macros in headers: %s",
            ", ".join(summary["unresolved_macro_headers"]),
        )

    return entry


async def parse_body(request: Request) -> tuple[Any, str]:
    raw = await request.body()
    raw_text = raw.decode("utf-8", errors="replace")
    if not raw_text:
        return {}, raw_text
    try:
        return json.loads(raw_text), raw_text
    except json.JSONDecodeError:
        return {"_raw": raw_text}, raw_text
