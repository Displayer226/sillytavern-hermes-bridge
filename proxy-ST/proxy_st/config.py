import os
import re
from pathlib import Path


def env_bool(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def parse_hermes_profile_allowlist(value: str | None) -> frozenset[str] | None:
    """Parse an optional allowlist; restricted mode must retain Hermes default."""
    if value is None:
        return None
    profiles = {item.strip().lower() for item in value.split(",") if item.strip()}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item) for item in profiles):
        raise ValueError("HERMES_PROFILE_ALLOWLIST contains an invalid profile name")
    if "default" not in profiles:
        raise ValueError("HERMES_PROFILE_ALLOWLIST must include the default profile")
    return frozenset(profiles)


def backend_env_float(backend_name: str, suffix: str, default: float) -> float:
    value = os.getenv(f"{backend_name.upper()}_{suffix}")
    if value is None:
        value = os.getenv(suffix)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalized_openai_base_url(name: str) -> str:
    raw = os.getenv(f"{name}_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return ""

    api_prefix = os.getenv(f"{name}_API_PREFIX", "/v1").strip()
    if not api_prefix:
        return raw

    normalized_prefix = "/" + api_prefix.strip("/")
    if raw.lower().endswith(normalized_prefix.lower()):
        return raw
    return f"{raw}{normalized_prefix}"


APP_NAME = "sillytavern-session-proxy"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.getenv("LOG_DIR", "/app/logs"))
LOG_JSON_BACKUP_COUNT = env_int("LOG_JSON_BACKUP_COUNT", "14")
LOG_BODY_MAX_CHARS = max(0, env_int("LOG_BODY_MAX_CHARS", "4000"))
LOG_INCLUDE_BODIES = env_bool("LOG_INCLUDE_BODIES", "false")
LOG_REQUEST_MAX_BYTES = max(1024, env_int("LOG_REQUEST_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_REQUEST_BACKUP_COUNT = max(0, env_int("LOG_REQUEST_BACKUP_COUNT", str(LOG_JSON_BACKUP_COUNT)))
DEFAULT_BACKEND = "hermes"
# Unset keeps the existing private behavior (all Hermes profiles remain
# selectable). Public deployments should set this explicitly, for example to
# "default" for the text-only Quick Start.
HERMES_PROFILE_ALLOWLIST = parse_hermes_profile_allowlist(os.getenv("HERMES_PROFILE_ALLOWLIST"))
WS_TOKEN = os.getenv("WS_TOKEN", "").strip()  # empty = no auth (dev default)
RELAY_TIMEOUT_SECONDS = env_float("RELAY_TIMEOUT_SECONDS", "180")
RELAY_CONNECT_TIMEOUT_SECONDS = env_float(
    "RELAY_CONNECT_TIMEOUT_SECONDS",
    str(min(RELAY_TIMEOUT_SECONDS, 30.0)),
)
STREAM_IDLE_TIMEOUT_SECONDS = env_float("STREAM_IDLE_TIMEOUT_SECONDS", str(RELAY_TIMEOUT_SECONDS))
STREAM_HEARTBEAT_SECONDS = env_float("STREAM_HEARTBEAT_SECONDS", "15")
HEALTH_CHECK_TIMEOUT_SECONDS = env_float("HEALTH_CHECK_TIMEOUT_SECONDS", "3")
# HTTP /models discovery is OPTIONAL. WebSocket is the mandatory bridge.
# Default false: readiness never probes HERMES_BASE_URL and /v1/models serves
# static PROXY_MODELS.
MODELS_FETCH_BACKENDS = env_bool("PROXY_FETCH_BACKEND_MODELS", "false")
MODELS_FETCH_TIMEOUT_SECONDS = env_float("PROXY_MODELS_TIMEOUT_SECONDS", "5")
MODELS = [
    model.strip()
    for model in os.getenv(
        "PROXY_MODELS",
        "hermes,hermes-log,sillytavern-log",
    ).split(",")
    if model.strip()
]

SESSION_PERSISTENCE_ENABLED = env_bool("SESSION_PERSISTENCE_ENABLED", "true")
SESSION_PERSISTENCE_BACKEND = os.getenv("SESSION_PERSISTENCE_BACKEND", "sqlite").strip().lower()
if SESSION_PERSISTENCE_BACKEND not in {"sqlite", "json"}:
    SESSION_PERSISTENCE_BACKEND = "sqlite"
SESSIONS_DATA_FILE = Path(os.getenv("SESSIONS_DATA_FILE", "data/sessions.json"))
SESSIONS_DB_FILE = Path(os.getenv("SESSIONS_DB_FILE", "data/sessions.sqlite3"))
SESSION_PERSISTENCE_INTERVAL = int(os.getenv("SESSION_PERSISTENCE_INTERVAL_SECONDS", "30"))

WORKSPACE_EXPLORER_ENABLED = env_bool("WORKSPACE_EXPLORER_ENABLED", "true")
WORKSPACE_ROOT_RAW = os.getenv("WORKSPACE_ROOT", os.getenv("HERMES_WORKSPACE_ROOT", "")).strip()
WORKSPACE_ROOT = Path(WORKSPACE_ROOT_RAW).expanduser() if WORKSPACE_ROOT_RAW else None
WORKSPACE_HOST_ROOT_RAW = os.getenv("WORKSPACE_HOST_ROOT", "").strip()
WORKSPACE_HOST_ROOT = Path(WORKSPACE_HOST_ROOT_RAW).expanduser() if WORKSPACE_HOST_ROOT_RAW else None
WORKSPACE_MAX_DEPTH = int(os.getenv("WORKSPACE_MAX_DEPTH", "3"))
WORKSPACE_MAX_ENTRIES = max(1, min(int(os.getenv("WORKSPACE_MAX_ENTRIES", "5000")), 10000))
WORKSPACE_PREVIEW_MAX_BYTES = int(os.getenv("WORKSPACE_PREVIEW_MAX_BYTES", "262144"))
WORKSPACE_EXCLUDE_NAMES = {
    name.strip()
    for name in os.getenv(
        "WORKSPACE_EXCLUDE_NAMES",
        ".git,node_modules,__pycache__,.venv,venv,.mypy_cache,.pytest_cache,.ruff_cache",
    ).split(",")
    if name.strip()
}

RATE_LIMITING_ENABLED = env_bool("RATE_LIMITING_ENABLED", "false")
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "60"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

CORS_ORIGINS_RAW = os.getenv("CORS_ORIGINS", "").strip()
if CORS_ORIGINS_RAW:
    CORS_ORIGINS = [o.strip() for o in CORS_ORIGINS_RAW.split(",") if o.strip()]
else:
    # Safe defaults: local SillyTavern instances (no wildcard)
    CORS_ORIGINS = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

MODELS_CACHE_TTL_SECONDS = int(os.getenv("MODELS_CACHE_TTL_SECONDS", "300"))

# ─── WebSocket subprotocol authentication ────────────────────────────
# The /ws endpoint authenticates through the WebSocket subprotocol list:
#   [<application subprotocol>, "auth.<base64url(UTF-8(WS_TOKEN))>"]
# base64url without padding, matching the companion extension client.
WS_APPLICATION_SUBPROTOCOL = "sillytavern-hermes-bridge"
WS_AUTH_SUBPROTOCOL_PREFIX = "auth."
# Temporary migration switch: the legacy client sent the token as a
# `?token=` URL query parameter. False (default) rejects it; true keeps
# accepting it until every client has migrated to subprotocol auth.
WS_QUERY_TOKEN_COMPAT = env_bool("PROXY_WS_QUERY_TOKEN_COMPAT", "false")

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "x-api-key",
    "api-key",
    "openai-api-key",
    "anthropic-api-key",
    "x-proxy-token",
}

SESSION_HEADER_CANDIDATES = (
    "x-st-session",
    "x-sillytavern-session",
    "x-session-id",
)

# URL query parameters redacted from logs (access logs and request logs).
SENSITIVE_QUERY_PARAMS = {
    "token",
    "access_token",
    "api_key",
    "key",
}

SESSION_BODY_CANDIDATES = (
    "st_proxy_session_id",
    "st_proxy_session",
    "st_session_id",
)
SESSION_BODY_OBJECT_CANDIDATES = (
    "st_proxy",
    "sillytavern_proxy",
)
SESSION_BODY_OBJECT_KEY_CANDIDATES = (
    "session_id",
    "session",
    "id",
    "chat_id",
    "chat",
)
BACKEND_BODY_CANDIDATES = (
    "st_proxy_backend",
)

BACKEND_CONFIGS = {
    "hermes": {
        "base_url": normalized_openai_base_url("HERMES"),
        "api_key": os.getenv("HERMES_API_KEY", ""),
        "model": os.getenv("HERMES_MODEL", "").strip(),
        "ws_url": os.getenv("HERMES_WS_URL", "ws://localhost:8642/api/ws"),
        "relay_timeout": backend_env_float("hermes", "RELAY_TIMEOUT_SECONDS", RELAY_TIMEOUT_SECONDS),
        "connect_timeout": backend_env_float("hermes", "RELAY_CONNECT_TIMEOUT_SECONDS", RELAY_CONNECT_TIMEOUT_SECONDS),
        "stream_idle_timeout": backend_env_float("hermes", "STREAM_IDLE_TIMEOUT_SECONDS", STREAM_IDLE_TIMEOUT_SECONDS),
        "stream_heartbeat_seconds": backend_env_float("hermes", "STREAM_HEARTBEAT_SECONDS", STREAM_HEARTBEAT_SECONDS),
    },
}

HERMES_DASHBOARD_URL = os.getenv("HERMES_DASHBOARD_URL", "http://localhost:9119/")
HERMES_DASHBOARD_AUTH_MODE = os.getenv("HERMES_DASHBOARD_AUTH_MODE", "auto").strip().lower()
if HERMES_DASHBOARD_AUTH_MODE not in {"auto", "password", "legacy", "none", "off"}:
    HERMES_DASHBOARD_AUTH_MODE = "auto"
HERMES_DASHBOARD_AUTH_PROVIDER = os.getenv("HERMES_DASHBOARD_AUTH_PROVIDER", "basic").strip() or "basic"
HERMES_DASHBOARD_AUTH_USERNAME = (
    os.getenv("HERMES_DASHBOARD_AUTH_USERNAME")
    or os.getenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME")
    or ""
).strip()
HERMES_DASHBOARD_AUTH_PASSWORD = (
    os.getenv("HERMES_DASHBOARD_AUTH_PASSWORD")
    or os.getenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD")
    or ""
)
HERMES_DASHBOARD_AUTH_TIMEOUT_SECONDS = env_float("HERMES_DASHBOARD_AUTH_TIMEOUT_SECONDS", "5")
HERMES_TOOL_PROGRESS_MODE = os.getenv("HERMES_TOOL_PROGRESS_MODE", "verbose").strip().lower()
if HERMES_TOOL_PROGRESS_MODE not in {"", "off", "new", "all", "verbose"}:
    HERMES_TOOL_PROGRESS_MODE = "verbose"
