import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Any

from .config import APP_NAME, LOG_DIR, LOG_JSON_BACKUP_COUNT, LOG_LEVEL, SENSITIVE_QUERY_PARAMS


_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def set_request_id(request_id: str) -> contextvars.Token[str | None]:
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    _request_id_var.reset(token)


def current_request_id() -> str | None:
    return _request_id_var.get()


def redact_sensitive_query(text: str) -> str:
    """Replace values of sensitive URL query parameters with a fixed mask."""
    if not text:
        return text
    pattern = "(" + "|".join(sorted(SENSITIVE_QUERY_PARAMS)) + ")"
    return re.sub(
        rf"([?&]){pattern}=([^&\s]*)",
        r"\1\2=***redacted***",
        text,
        flags=re.IGNORECASE,
    )


class SensitiveQueryRedactionFilter(logging.Filter):
    """Redact sensitive URL query parameters from log messages.

    Applied to (among others) Uvicorn access logs, whose formatted message
    embeds the full request line, including the query string of rejected
    WebSocket handshake requests. Subprotocol header values are never part
    of access-log lines and are never written by this filter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_query(str(record.msg))
        if record.args:
            record.args = tuple(
                redact_sensitive_query(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


_REDACTED_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error")


def install_sensitive_query_redaction() -> None:
    """Attach the redaction filter to the root and Uvicorn handlers, idempotently.

    Uvicorn configures its loggers with `propagate=False`, so access logs only
    pass through handlers attached directly to `uvicorn`/`uvicorn.access`;
    those handlers must carry the filter too. Safe to call multiple times
    (e.g. at import and again from tests): existing filters are reused and
    never duplicated.
    """
    for name in _REDACTED_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, SensitiveQueryRedactionFilter) for f in handler.filters):
                handler.addFilter(SensitiveQueryRedactionFilter())


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _coerce_level(level_name: str) -> int:
    level = getattr(logging, str(level_name or "").upper(), None)
    return level if isinstance(level, int) else logging.INFO


def _configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_proxy_st_configured", False):
        root.setLevel(_coerce_level(LOG_LEVEL))
        return

    root.handlers.clear()
    root.setLevel(_coerce_level(LOG_LEVEL))

    request_filter = RequestIdFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s")
    )
    console_handler.addFilter(request_filter)
    root.addHandler(console_handler)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        json_handler = TimedRotatingFileHandler(
            LOG_DIR / "app.jsonl",
            when="midnight",
            backupCount=LOG_JSON_BACKUP_COUNT,
            encoding="utf-8",
            utc=True,
        )
        json_handler.setFormatter(JsonFormatter())
        json_handler.addFilter(request_filter)
        root.addHandler(json_handler)
    except OSError:
        root.exception("failed to initialize JSON log handler")

    root._proxy_st_configured = True


_configure_logging()
install_sensitive_query_redaction()
logger = logging.getLogger(APP_NAME)
