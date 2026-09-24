import asyncio
import os
import resource
import time
from typing import Any

import httpx

from .config import (
    APP_NAME,
    BACKEND_CONFIGS,
    DEFAULT_BACKEND,
    HEALTH_CHECK_TIMEOUT_SECONDS,
    MODELS_FETCH_BACKENDS,
)
from .request_transform import backend_auth_headers, backend_endpoint


_STARTED_AT = time.time()


def _rss_bytes() -> int:
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as handle:
            rss_pages = int(handle.read().split()[1])
        return rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux reports KiB, macOS reports bytes. The container target is Linux,
        # but keep the fallback sane for local development.
        return int(usage.ru_maxrss * 1024 if usage.ru_maxrss < 10**10 else usage.ru_maxrss)


def process_metrics() -> dict[str, Any]:
    now = time.time()
    uptime = max(0.0, now - _STARTED_AT)
    cpu_seconds = time.process_time()
    return {
        "pid": os.getpid(),
        "uptime_seconds": round(uptime, 3),
        "rss_bytes": _rss_bytes(),
        "cpu_seconds": round(cpu_seconds, 6),
        "cpu_percent_avg": round((cpu_seconds / uptime) * 100, 3) if uptime > 0 else 0.0,
        "started_at": _STARTED_AT,
    }


async def _check_backend_models(backend_name: str, config: dict[str, Any]) -> dict[str, Any]:
    base_url = config.get("base_url")
    if not base_url:
        return {"configured": False, "ok": True, "status": "disabled"}

    url = backend_endpoint(str(base_url), "/models")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=backend_auth_headers(config))
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "configured": True,
            "ok": 200 <= response.status_code < 500,
            "status": "reachable" if response.status_code < 500 else "unhealthy",
            "status_code": response.status_code,
            "latency_ms": latency_ms,
        }
    except httpx.HTTPError:
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "configured": True,
            "ok": False,
            "status": "unreachable",
            "error_type": "http_error",
            "latency_ms": latency_ms,
        }


async def readiness_snapshot(hermes_ws_manager: Any) -> dict[str, Any]:
    # HTTP /models discovery is optional; WebSocket is the mandatory bridge.
    # When discovery is disabled the readiness probe never touches
    # HERMES_BASE_URL, so an absent or unreachable HTTP endpoint cannot fail
    # readiness — websocket_ready alone decides the HTTP readiness verdict.
    backends: dict[str, Any] = {}
    if not MODELS_FETCH_BACKENDS:
        backends["hermes"] = {
            "configured": bool(BACKEND_CONFIGS["hermes"].get("base_url")),
            "ok": True,
            "status": "disabled",
            "reason": "model_discovery_disabled",
        }
    else:
        checks = await asyncio.gather(
            *[
                _check_backend_models(name, config)
                for name, config in BACKEND_CONFIGS.items()
            ],
            return_exceptions=True,
        )

        for backend_name, check in zip(BACKEND_CONFIGS, checks):
            if isinstance(check, Exception):
                backends[backend_name] = {
                    "configured": bool(BACKEND_CONFIGS[backend_name].get("base_url")),
                    "ok": False,
                    "status": "error",
                    "error_type": "probe_error",
                }
            else:
                backends[backend_name] = check

    hermes = backends.setdefault("hermes", {"configured": False, "ok": True, "status": "disabled"})
    hermes["websocket_connected"] = bool(getattr(hermes_ws_manager, "is_connected", False))
    hermes["websocket_ready"] = bool(getattr(hermes_ws_manager, "is_ready", False))
    hermes["websocket_sessions"] = int(getattr(hermes_ws_manager, "session_count", 0))
    if BACKEND_CONFIGS["hermes"].get("ws_url"):
        hermes["configured"] = True
        hermes["ok"] = bool(hermes["ok"] and hermes["websocket_ready"])
        if hermes["ok"]:
            hermes["status"] = "ready"
        elif not hermes["websocket_ready"]:
            hermes["status"] = "not_ready"
        # WebSocket ready but the HTTP check failed: keep the probe status
        # ("unreachable"/"unhealthy") so the payload explains the failure.

    configured_checks = [check for check in backends.values() if check.get("configured")]
    ready = all(check.get("ok") for check in configured_checks)
    return {
        "status": "ready" if ready else "not_ready",
        "service": APP_NAME,
        "default_backend": DEFAULT_BACKEND,
        "model_discovery_enabled": MODELS_FETCH_BACKENDS,
        "ready": ready,
        "backends": backends,
        "process": process_metrics(),
    }


def prometheus_metrics(ready_snapshot: dict[str, Any], session_count: int, tool_call_count: int) -> str:
    process = process_metrics()
    lines = [
        "# HELP proxy_process_uptime_seconds Process uptime in seconds.",
        "# TYPE proxy_process_uptime_seconds gauge",
        f"proxy_process_uptime_seconds {process['uptime_seconds']}",
        "# HELP proxy_process_rss_bytes Resident set size in bytes.",
        "# TYPE proxy_process_rss_bytes gauge",
        f"proxy_process_rss_bytes {process['rss_bytes']}",
        "# HELP proxy_process_cpu_seconds CPU seconds consumed by the process.",
        "# TYPE proxy_process_cpu_seconds counter",
        f"proxy_process_cpu_seconds {process['cpu_seconds']}",
        "# HELP proxy_sessions_total In-memory proxy sessions.",
        "# TYPE proxy_sessions_total gauge",
        f"proxy_sessions_total {session_count}",
        "# HELP proxy_tool_calls_total In-memory captured tool calls.",
        "# TYPE proxy_tool_calls_total gauge",
        f"proxy_tool_calls_total {tool_call_count}",
        "# HELP proxy_ready Readiness status where 1 is ready.",
        "# TYPE proxy_ready gauge",
        f"proxy_ready {1 if ready_snapshot.get('ready') else 0}",
    ]

    for backend_name, check in ready_snapshot.get("backends", {}).items():
        configured = 1 if check.get("configured") else 0
        ok = 1 if check.get("ok") else 0
        lines.append(f'proxy_backend_configured{{backend="{backend_name}"}} {configured}')
        lines.append(f'proxy_backend_ready{{backend="{backend_name}"}} {ok}')

    return "\n".join(lines) + "\n"
