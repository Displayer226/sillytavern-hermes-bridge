import asyncio
import base64
import binascii
import json
import re
import secrets
from contextlib import asynccontextmanager

import uuid

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .auth import is_public_http_path, is_worker_voice_path, request_has_proxy_token
from .config import (
    APP_NAME,
    BACKEND_CONFIGS,
    CORS_ORIGINS,
    DEFAULT_BACKEND,
    HERMES_DASHBOARD_AUTH_MODE,
    HERMES_DASHBOARD_AUTH_PASSWORD,
    HERMES_DASHBOARD_AUTH_PROVIDER,
    HERMES_DASHBOARD_AUTH_TIMEOUT_SECONDS,
    HERMES_DASHBOARD_AUTH_USERNAME,
    HERMES_DASHBOARD_URL,
    MODELS,
    MODELS_FETCH_BACKENDS,
    WS_AUTH_SUBPROTOCOL_PREFIX,
    WS_APPLICATION_SUBPROTOCOL,
    WS_QUERY_TOKEN_COMPAT,
    WS_TOKEN,
)
from .dummy import chat_completion_response, stream_response
from .hermes_ws import HermesWebSocketManager
from .health import process_metrics, prometheus_metrics, readiness_snapshot
from .log import logger, reset_request_id, set_request_id
from .logging_utils import log_request, parse_body
from .mcp import frontend_tool_broker, handle_mcp_http_payload, mcp_response_headers, mcp_sse_keepalive
from .models import list_proxy_models
from .persistence import _load_sessions, _save_sessions, _shutdown_persist, _startup_persist_loop
from .profile_policy import (
    PROFILE_NOT_ALLOWED_CODE,
    PROFILE_NOT_ALLOWED_MESSAGE,
    ProfileNotAllowedError,
    profile_not_allowed_body,
)
from .rate_limit import _check_rate_limit, _rate_limit_response
from .realtime import ws_broadcast, ws_subscribe, ws_unsubscribe, ws_unsubscribe_all
from .relay import forward_to_backend
from .schemas import HealthResponse, ModelListResponse, ReadinessResponse, SetModelRequest, SetProfileRequest, ToolCallsPage
from .state import SESSION_INFOS, SESSION_TOOL_CALLS
from .tool_calls import clear_session_tool_calls, filter_tool_calls, merge_live_session_info
from .utils import now_iso
from .workspace import list_workspace_tree, preview_workspace_file, resolve_workspace_path
from .voice import router as voice_router, active_call


hermes_ws_manager = HermesWebSocketManager(
    ws_url=BACKEND_CONFIGS["hermes"]["ws_url"],
    dashboard_url=HERMES_DASHBOARD_URL,
    dashboard_auth_mode=HERMES_DASHBOARD_AUTH_MODE,
    dashboard_auth_provider=HERMES_DASHBOARD_AUTH_PROVIDER,
    dashboard_auth_username=HERMES_DASHBOARD_AUTH_USERNAME,
    dashboard_auth_password=HERMES_DASHBOARD_AUTH_PASSWORD,
    dashboard_auth_timeout=HERMES_DASHBOARD_AUTH_TIMEOUT_SECONDS,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # ── Startup ──
    _load_sessions()
    await _startup_persist_loop()
    await hermes_ws_manager.start()
    logger.info("Hermes WebSocket manager started")
    yield
    # ── Shutdown ──
    await hermes_ws_manager.stop()
    logger.info("Hermes WebSocket manager stopped")
    await _shutdown_persist()


app = FastAPI(title="SillyTavern Session Proxy", version="0.1.0", lifespan=_lifespan)
app.include_router(voice_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)


# ── Request body size limit ─────────────────────────────────────────
_MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MiB


class _BodyTooLarge(Exception):
    pass


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-proxy-request-id")
        or uuid.uuid4().hex
    )
    request.state.request_id = request_id
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled request error")
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Proxy internal error",
                    "type": "proxy_error",
                    "code": "internal_error",
                    "request_id": request_id,
                }
            },
        )
    finally:
        reset_request_id(token)
    response.headers["X-Request-Id"] = request_id
    return response


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "message": "Request body too large",
                                "type": "proxy_error",
                                "code": "body_too_large",
                            }
                        },
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": "Invalid Content-Length header",
                            "type": "proxy_error",
                            "code": "invalid_content_length",
                        }
                    },
                )

        received = 0
        original_receive = request._receive

        async def receive_with_limit():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > _MAX_BODY_BYTES:
                    raise _BodyTooLarge()
            return message

        request._receive = receive_with_limit
        try:
            return await call_next(request)
        except _BodyTooLarge:
            return JSONResponse(
                status_code=413,
                content={"error": {"message": "Request body too large", "type": "proxy_error", "code": "body_too_large"}},
            )
    return await call_next(request)


@app.middleware("http")
async def _proxy_auth_middleware(request: Request, call_next):
    if (
        WS_TOKEN
        and request.method != "OPTIONS"
        and not is_public_http_path(request.url.path)
        and not is_worker_voice_path(request.url.path)
        and not request_has_proxy_token(request, WS_TOKEN)
    ):
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Proxy authentication required", "type": "proxy_error", "code": "proxy_auth_required"}},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)

# hermes_ws_manager defined above, before lifespan


_WS_AUTH_SUBPROTOCOL_RE = re.compile(r"[A-Za-z0-9_-]+")


def _decode_ws_auth_subprotocol(value: str) -> bytes | None:
    """Decode an `auth.<base64url>` subprotocol token; None when malformed.

    base64url alphabet without padding, per the bridge client convention.
    Strictly rejects padding, whitespace, and characters outside the
    base64url alphabet.
    """
    encoded = value[len(WS_AUTH_SUBPROTOCOL_PREFIX):]
    if not encoded or not _WS_AUTH_SUBPROTOCOL_RE.fullmatch(encoded):
        return None
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (binascii.Error, ValueError):
        return None


def _ws_subprotocol_auth(ws: WebSocket, token: str) -> bool:
    """Validate the `ws.scope` subprotocol list against `token`.

    Strict: requires the fixed application subprotocol exactly once, plus
    one well-formed `auth.` subprotocol whose decoded value matches the
    configured token. Never selects or logs the auth subprotocol.
    """
    subprotocols = list(ws.scope.get("subprotocols") or [])
    app_protocols = [p for p in subprotocols if p == WS_APPLICATION_SUBPROTOCOL]
    if len(app_protocols) != 1:
        return False
    expected = token.encode("utf-8")
    seen_auth = False
    for protocol in subprotocols:
        if protocol == WS_APPLICATION_SUBPROTOCOL:
            continue
        if not protocol.startswith(WS_AUTH_SUBPROTOCOL_PREFIX):
            return False
        if seen_auth:
            return False
        decoded = _decode_ws_auth_subprotocol(protocol)
        if decoded is None or not secrets.compare_digest(decoded, expected):
            return False
        seen_auth = True
    return seen_auth


def _ws_offered_application_subprotocol(subprotocols: list[str]) -> bool:
    """True when the client offered exactly the application protocol once.

    With no token configured there is nothing to authenticate, so the only
    negotiation question left is whether the client explicitly offered the
    application protocol; anything else (missing, duplicated, or extra
    protocols) must never be selected.
    """
    return len([p for p in subprotocols if p == WS_APPLICATION_SUBPROTOCOL]) == 1


async def _ws_authenticate(ws: WebSocket) -> tuple[bool, str | None]:
    """Authenticate /ws and pick the subprotocol to echo in ``ws.accept()``.

    Returns ``(authenticated, selected_subprotocol)``. The selected
    subprotocol is always one the client offered, or ``None`` to accept
    without any:

    - modern subprotocol auth selects only ``WS_APPLICATION_SUBPROTOCOL``;
    - a valid legacy ``?token=`` (PROXY_WS_QUERY_TOKEN_COMPAT) selects none,
      because the legacy client offers no subprotocols at all;
    - with no WS_TOKEN configured, an exactly-offered application protocol
      may be selected, otherwise none;
    - an unoffered protocol is never selected.
    """
    subprotocols = list(getattr(ws, "scope", {}).get("subprotocols") or [])
    if not WS_TOKEN:
        offered = _ws_offered_application_subprotocol(subprotocols)
        return True, WS_APPLICATION_SUBPROTOCOL if offered else None
    if _ws_subprotocol_auth(ws, WS_TOKEN):
        return True, WS_APPLICATION_SUBPROTOCOL
    if WS_QUERY_TOKEN_COMPAT:
        query_token = ws.query_params.get("token")
        if query_token and secrets.compare_digest(query_token, WS_TOKEN):
            return True, None
    logger.warning("ws rejected: invalid or missing auth subprotocol")
    return False, None


async def _combined_session_info(session_id: str) -> dict:
    live_info = None
    try:
        live_info = await hermes_ws_manager.get_session_info(session_id)
    except (RuntimeError, asyncio.TimeoutError, ConnectionError) as exc:
        logger.debug("Hermes live info unavailable for session %s: %s", session_id, exc)
    hermes_session_status = hermes_ws_manager.session_status(session_id)
    info = merge_live_session_info(session_id, live_info)
    hermes_info = info.get("hermes") if isinstance(info.get("hermes"), dict) else {}
    return {
        "session_id": session_id,
        "hermes_session_status": hermes_session_status,
        "tool_calls": SESSION_TOOL_CALLS.get(session_id, []),
        "last_usage": info.get("last_usage"),
        "total_requests": info.get("total_requests", 0),
        "agent_status": info.get("agent_status") or {"active": False},
        "model": info.get("model"),
        "reasoning_effort": info.get("reasoning_effort"),
        "profile": hermes_info.get("profile_name") or info.get("profile"),
        "hermes": info.get("hermes"),
    }


async def _delete_session_data(session_id: str) -> None:
    try:
        await hermes_ws_manager.close_session(session_id)
    except (RuntimeError, asyncio.TimeoutError, ConnectionError):
        logger.exception("Failed to close Hermes session for ST=%s", session_id)
    SESSION_TOOL_CALLS.pop(session_id, None)
    SESSION_INFOS.pop(session_id, None)
    _save_sessions()
    await ws_broadcast(session_id, {
        "type": "session_deleted",
        "session_id": session_id,
    })


@app.get("/health", response_model=HealthResponse)
async def health():
    backends = {
        name: {
            "configured": bool(config.get("base_url")),
            "base_url": config.get("base_url") or None,
            "model": config.get("model") or None,
            "api_key_configured": bool(config.get("api_key")),
        }
        for name, config in BACKEND_CONFIGS.items()
    }
    backends["hermes"]["websocket_connected"] = hermes_ws_manager.is_connected
    backends["hermes"]["websocket_ready"] = hermes_ws_manager.is_ready
    backends["hermes"]["websocket_sessions"] = hermes_ws_manager.session_count
    return {
        "status": "ok",
        "service": APP_NAME,
        "default_backend": DEFAULT_BACKEND,
        "models_fetch_backends": MODELS_FETCH_BACKENDS,
        "backends": backends,
        "process": process_metrics(),
    }


@app.get("/health/live", response_model=HealthResponse)
async def health_live():
    return {
        "status": "ok",
        "service": APP_NAME,
        "process": process_metrics(),
    }


@app.get("/health/ready", response_model=ReadinessResponse)
async def health_ready():
    snapshot = await readiness_snapshot(hermes_ws_manager)
    return JSONResponse(snapshot, status_code=200 if snapshot["ready"] else 503)


@app.get("/metrics")
async def metrics():
    snapshot = await readiness_snapshot(hermes_ws_manager)
    session_count = len(SESSION_INFOS)
    tool_call_count = sum(len(calls) for calls in SESSION_TOOL_CALLS.values())
    return Response(
        prometheus_metrics(snapshot, session_count, tool_call_count),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Authenticate via WebSocket subprotocols; optionally accept the legacy
    # `?token=` query parameter while PROXY_WS_QUERY_TOKEN_COMPAT is enabled.
    # On success authentication also decides the subprotocol echoed in
    # accept(): always a protocol the client offered, or none at all.
    authenticated, selected_subprotocol = await _ws_authenticate(ws)
    if not authenticated:
        await ws.close(code=4001, reason="Unauthorized")
        return
    if not WS_TOKEN and CORS_ORIGINS and "*" not in CORS_ORIGINS:
        origin = ws.headers.get("origin")
        if origin and origin not in CORS_ORIGINS:
            logger.warning(f"ws rejected: origin {origin} not allowed")
            await ws.close(code=4003, reason="Forbidden")
            return
    if selected_subprotocol:
        await ws.accept(subprotocol=selected_subprotocol)
    else:
        await ws.accept()
    connection_id = uuid.uuid4().hex[:12]
    logger.info("ws connected connection_id=%s", connection_id)
    try:
        await ws.send_json({"type": "connected", "connection_id": connection_id})
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")

            if msg_type == "subscribe":
                session_id = msg.get("session_id")
                if not session_id:
                    await ws.send_json({"type": "error", "message": "Missing session_id"})
                    continue
                ws_subscribe(ws, session_id)
                calls = SESSION_TOOL_CALLS.get(session_id, [])
                info = await _combined_session_info(session_id)
                await ws.send_json({
                    "type": "subscribed",
                    "session_id": session_id,
                    "tool_calls": calls,
                    "session_info": info,
                    "server_requests": hermes_ws_manager.server_request_snapshot(session_id),
                })
                logger.info("ws subscribe connection_id=%s session=%s", connection_id, session_id)

            elif msg_type == "unsubscribe":
                session_id = msg.get("session_id")
                if session_id:
                    ws_unsubscribe(ws, session_id)
                    await ws.send_json({"type": "unsubscribed", "session_id": session_id})
                else:
                    ws_unsubscribe_all(ws)
                    await ws.send_json({"type": "unsubscribed", "session_id": "*"})

            elif msg_type == "get_session_info":
                session_id = msg.get("session_id")
                if not session_id:
                    await ws.send_json({"type": "error", "message": "Missing session_id"})
                    continue
                calls = SESSION_TOOL_CALLS.get(session_id, [])
                info = await _combined_session_info(session_id)
                info["tool_calls"] = calls
                info["type"] = "session_info"
                await ws.send_json(info)

            elif msg_type == "clear_tool_calls":
                session_id = msg.get("session_id")
                if not session_id:
                    await ws.send_json({"type": "error", "message": "Missing session_id"})
                    continue
                await clear_session_tool_calls(session_id, reason="manual")

            elif msg_type == "delete_session":
                session_id = msg.get("session_id")
                if not session_id:
                    await ws.send_json({"type": "error", "message": "Missing session_id"})
                    continue
                await _delete_session_data(session_id)
                await ws.send_json({"type": "session_deleted", "session_id": session_id})

            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

            elif msg_type == "get_model_options":
                session_id = msg.get("session_id") or None
                options = await hermes_ws_manager.model_options(session_id)
                await ws.send_json({
                    "type": "model_options",
                    "session_id": session_id,
                    "options": options or {},
                })

            elif msg_type == "get_profile_options":
                options = await hermes_ws_manager.profile_options()
                await ws.send_json({
                    "type": "profile_options",
                    "options": options or {"active": "default", "profiles": []},
                })

            elif msg_type == "set_profile":
                session_id = msg.get("session_id")
                profile = msg.get("profile")
                if not session_id or not profile:
                    await ws.send_json({"type": "error", "message": "Missing session_id or profile"})
                    continue
                try:
                    result = await hermes_ws_manager.set_profile(session_id, profile)
                    await ws.send_json({
                        "type": "profile_changed",
                        "session_id": session_id,
                        **result,
                    })
                except ProfileNotAllowedError:
                    await ws.send_json({
                        "type": "error",
                        "code": PROFILE_NOT_ALLOWED_CODE,
                        "message": PROFILE_NOT_ALLOWED_MESSAGE,
                    })
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": str(exc)})

            elif msg_type == "set_model":
                session_id = msg.get("session_id")
                model = msg.get("model")
                if not session_id or not model:
                    await ws.send_json({"type": "error", "message": "Missing session_id or model"})
                    continue
                try:
                    result = None
                    if hermes_ws_manager.is_connected:
                        try:
                            if hermes_ws_manager.get_session(session_id):
                                result = await hermes_ws_manager.set_model(session_id, model)
                            else:
                                result = {"key": "model", "value": model, "deferred": True}
                        except Exception as hermes_exc:
                            logger.debug("Hermes model switch failed (might not be using Hermes): %s", hermes_exc)

                    # Update local SESSION_INFOS as fallback
                    session_info = SESSION_INFOS.setdefault(session_id, {"last_usage": None, "total_requests": 0})
                    session_info["model"] = model
                    session_info["updated_at"] = now_iso()
                    _save_sessions()

                    info = await _combined_session_info(session_id)
                    await ws.send_json({
                        "type": "model_changed",
                        "session_id": session_id,
                        "model": result.get("value") if (isinstance(result, dict) and result.get("value")) else model,
                        "result": result,
                        "session_info": info,
                    })
                    await ws_broadcast(session_id, {
                        "type": "session_info",
                        **info,
                    })
                except Exception as exc:
                    await ws.send_json({"type": "error", "message": f"Model switch failed: {exc}"})

            elif msg_type == "server_request_response":
                try:
                    await hermes_ws_manager.respond_server_request(
                        ws,
                        msg.get("session_id"),
                        msg.get("rpc_id"),
                        msg.get("method"),
                        msg.get("result"),
                    )
                except (ValueError, RuntimeError, asyncio.TimeoutError, ConnectionError):
                    # The manager emits a correlated, sanitized outcome for
                    # requests it owns. Do not turn it into an uncorrelated
                    # browser error or echo any transport detail.
                    continue

            elif msg_type == "server_request_clarify_lock":
                try:
                    result = await hermes_ws_manager.lock_server_request(
                        ws,
                        msg.get("session_id"),
                        msg.get("rpc_id"),
                        msg.get("question_id"),
                        msg.get("answer"),
                    )
                    if result.get("status") == "ok":
                        await ws.send_json({
                            "type": "server_request_clarify_lock_ack",
                            "session_id": msg.get("session_id"),
                            "rpc_id": msg.get("rpc_id"),
                            "remaining": result.get("remaining", []),
                        })
                except (ValueError, RuntimeError, asyncio.TimeoutError, ConnectionError):
                    continue

            elif msg_type == "agent_control":
                session_id = msg.get("session_id")
                action = str(msg.get("action") or "").strip().lower()
                client_request_id = msg.get("request_id")
                if not session_id or not action:
                    await ws.send_json({"type": "error", "message": "Missing session_id or action"})
                    continue

                try:
                    if action == "interrupt":
                        result = await hermes_ws_manager.interrupt_session(session_id)
                    elif action == "steer":
                        result = await hermes_ws_manager.steer_session(session_id, msg.get("text") or "")
                    elif action == "undo":
                        result = await hermes_ws_manager.undo_session(session_id)
                    elif action == "compress":
                        result = await hermes_ws_manager.compress_session(session_id, msg.get("focus_topic") or "")
                    else:
                        await ws.send_json({"type": "error", "message": f"Unknown agent control action: {action}"})
                        continue

                    info = await _combined_session_info(session_id)
                    await ws.send_json({
                        "type": "agent_control_result",
                        "session_id": session_id,
                        "action": action,
                        "request_id": client_request_id,
                        "status": "ok",
                        "result": result,
                        "session_info": info,
                    })
                    await ws_broadcast(session_id, {
                        "type": "session_info",
                        **info,
                    })
                except Exception as exc:
                    await ws.send_json({
                        "type": "agent_control_result",
                        "session_id": session_id,
                        "action": action,
                        "request_id": client_request_id,
                        "status": "error",
                        "message": str(exc),
                    })

            elif msg_type == "frontend_tool_result":
                request_id = str(msg.get("request_id") or "").strip()
                if not request_id:
                    await ws.send_json({"type": "error", "message": "Missing request_id"})
                    continue
                accepted = frontend_tool_broker.complete(request_id, {
                    "status": msg.get("status") or "ok",
                    "session_id": msg.get("session_id"),
                    "tool_name": msg.get("tool_name"),
                    "message": msg.get("message"),
                    "result": msg.get("result"),
                    "character": msg.get("character"),
                    "version": msg.get("version"),
                })
                await ws.send_json({
                    "type": "frontend_tool_result_ack",
                    "request_id": request_id,
                    "accepted": accepted,
                })

            else:
                await ws.send_json({"type": "error", "message": f"Unknown command: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("ws disconnected connection_id=%s", connection_id)
    except (RuntimeError, asyncio.TimeoutError, OSError, ConnectionError) as exc:
        logger.error("ws error connection_id=%s: %s", connection_id, exc)
    finally:
        ws_unsubscribe_all(ws)


@app.get("/v1/models", response_model=ModelListResponse)
@app.post("/v1/models", response_model=ModelListResponse)
async def models(request: Request, refresh: bool = False):
    await log_request(request, {}, "")
    model_result = await list_proxy_models(refresh=refresh)
    return {
        "object": "list",
        "data": model_result["data"],
        "cache_age": model_result["cache_age"],
        "cache_hit": model_result["cache_hit"],
        "cache_ttl": model_result["cache_ttl"],
    }


@app.get("/session/{session_id}/tool_calls", response_model=ToolCallsPage)
@app.get("/v1/session/{session_id}/tool_calls", response_model=ToolCallsPage)
async def get_session_tool_calls(
    session_id: str,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    tool: str | None = None,
    q: str | None = None,
):
    calls = SESSION_TOOL_CALLS.get(session_id, [])
    return filter_tool_calls(calls, limit=limit, offset=offset, status=status, tool=tool, q=q)


@app.delete("/session/{session_id}/tool_calls")
@app.delete("/v1/session/{session_id}/tool_calls")
async def delete_session_tool_calls(session_id: str):
    removed = await clear_session_tool_calls(session_id, reason="manual")
    return {"status": "cleared", "removed": removed}


@app.get("/session/{session_id}/info")
@app.get("/v1/session/{session_id}/info")
async def get_session_info(session_id: str):
    return await _combined_session_info(session_id)


@app.delete("/session/{session_id}")
@app.delete("/v1/session/{session_id}")
async def delete_session(session_id: str):
    await _delete_session_data(session_id)
    return {"status": "deleted"}


@app.get("/v1/hermes/model_options")
async def hermes_model_options(session_id: str | None = None):
    options = await hermes_ws_manager.model_options(session_id)
    return options or {"providers": [], "models": []}


@app.get("/v1/hermes/profile_options")
async def hermes_profile_options():
    options = await hermes_ws_manager.profile_options()
    return options or {"active": "default", "profiles": []}


@app.post("/v1/session/{session_id}/profile")
async def set_session_profile(session_id: str, payload: SetProfileRequest):
    try:
        return await hermes_ws_manager.set_profile(session_id, payload.profile)
    except ProfileNotAllowedError:
        return JSONResponse(profile_not_allowed_body(), status_code=403)
    except ValueError as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "profile_error", "code": "profile_switch_failed"}},
            status_code=400,
        )
    except Exception as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "proxy_backend_error", "code": "profile_switch_failed"}},
            status_code=502,
        )


@app.post("/v1/session/{session_id}/model")
async def set_session_model(session_id: str, payload: SetModelRequest):
    model = payload.model.strip()
    try:
        if hermes_ws_manager.get_session(session_id):
            result = await hermes_ws_manager.set_model(session_id, model)
        else:
            result = {"key": "model", "value": model, "deferred": True}
            session_info = SESSION_INFOS.setdefault(
                session_id, {"last_usage": None, "total_requests": 0}
            )
            session_info["model"] = model
            session_info["updated_at"] = now_iso()
            _save_sessions()
    except Exception as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "proxy_backend_error", "code": "model_switch_failed"}},
            status_code=502,
        )
    info = await _combined_session_info(session_id)
    await ws_broadcast(session_id, {"type": "session_info", **info})
    return {"result": result, "session_info": info}


@app.post("/mcp")
@app.post("/mcp/")
async def mcp_endpoint(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
            headers=mcp_response_headers(),
        )
    status_code, response_payload = await handle_mcp_http_payload(payload)
    if response_payload is None:
        return Response(status_code=status_code, headers=mcp_response_headers())
    return JSONResponse(response_payload, status_code=status_code, headers=mcp_response_headers())


@app.get("/mcp")
@app.get("/mcp/")
async def mcp_sse_endpoint():
    return StreamingResponse(
        mcp_sse_keepalive(),
        media_type="text/event-stream",
        headers={
            **mcp_response_headers(),
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/session/{session_id}/workspace/tree")
@app.get("/v1/session/{session_id}/workspace/tree")
async def get_workspace_tree(session_id: str, path: str = "", depth: int | None = None):
    await _combined_session_info(session_id)
    return list_workspace_tree(session_id, path, depth)


@app.get("/session/{session_id}/workspace/file")
@app.get("/v1/session/{session_id}/workspace/file")
async def get_workspace_file(session_id: str, path: str):
    await _combined_session_info(session_id)
    return preview_workspace_file(session_id, path)


@app.get("/session/{session_id}/workspace/download")
@app.get("/v1/session/{session_id}/workspace/download")
async def download_workspace_file(session_id: str, path: str):
    await _combined_session_info(session_id)
    _, target = resolve_workspace_path(session_id, path)
    if not target.exists() or not target.is_file():
        return JSONResponse(
            {"error": {"message": "Workspace file not found", "type": "proxy_error", "code": "workspace_file_not_found"}},
            status_code=404,
        )
    return FileResponse(target, filename=target.name)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body, raw_body = await parse_body(request)
    entry = await log_request(request, body, raw_body)

    session_id = entry["summary"]["session_id"]

    if active_call(session_id):
        return JSONResponse(status_code=409, content={"error": {"message": "End the voice call before sending text in this chat"}})

    if not _check_rate_limit(session_id):
        return _rate_limit_response(session_id)

    if isinstance(body, dict):
        req_model = body.get("model")
        if req_model == "responses_proxy_get_tool_calls":
            session_id = body.get("session_id") or entry["summary"]["session_id"]
            calls = SESSION_TOOL_CALLS.get(session_id, [])
            return JSONResponse({"tool_calls": calls})
        if req_model == "responses_proxy_clear_tool_calls":
            session_id = body.get("session_id") or entry["summary"]["session_id"]
            removed = await clear_session_tool_calls(session_id, reason="manual")
            return JSONResponse({"status": "cleared", "removed": removed})

    try:
        backend_response = await forward_to_backend(entry, body, hermes_ws_manager)
    except httpx.HTTPError as exc:
        logger.exception("backend relay failed for %s", entry["request_id"])
        return JSONResponse(
            {
                "error": {
                    "message": f"Backend relay failed: {exc}",
                    "type": "proxy_backend_error",
                    "param": None,
                    "code": "backend_relay_failed",
                    "request_id": entry["request_id"],
                }
            },
            status_code=502,
        )

    if backend_response is not None:
        return backend_response

    model = body.get("model") if isinstance(body, dict) else None
    model = model or MODELS[0]
    summary = entry["summary"]
    content = (
        "[proxy-ST] SillyTavern request received and logged. "
        f"session={summary['session_id']} backend={summary['backend']} "
        f"messages={summary['message_count']} request_id={entry['request_id']}"
    )

    if isinstance(body, dict) and body.get("stream"):
        return StreamingResponse(stream_response(model, content), media_type="text/event-stream")

    return JSONResponse(chat_completion_response(model, content))


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(path: str, request: Request):
    body, raw_body = await parse_body(request)
    await log_request(request, body, raw_body)
    return JSONResponse(
        {
            "error": {
                "message": f"Unhandled path: /{path}",
                "type": "proxy_unhandled_path",
                "param": None,
                "code": "unhandled_path",
            }
        },
        status_code=404,
    )
