"""Manage a persistent WebSocket connection to the Hermes tui_gateway JSON-RPC API.

Maintains one WebSocket connection, manages multiple Hermes sessions, and
provides an async interface for submitting prompts and streaming responses.
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import quote

import httpx
import websockets

# Static import of websockets.protocol.State — available in websockets >=14.
# For older versions, falls back to .open attribute check.
try:
    from websockets.protocol import State as WSState
except ImportError:
    WSState = None

from .config import BACKEND_CONFIGS, HERMES_TOOL_PROGRESS_MODE
from .hermes_auth import (
    DashboardAuthError,
    HermesDashboardAuthenticator,
    HermesDashboardPasswordAuth,
)
from .persistence import _save_sessions
from .profile_policy import (
    ProfileNotAllowedError,
    is_profile_allowed,
    profile_allowlist,
    profile_options_for_client,
    resolve_profile_selection,
    session_profile_override,
    stored_profile_selection,
)
from .realtime import ws_broadcast, ws_is_subscribed
from .state import SESSION_INFOS
from .utils import now_iso

logger = logging.getLogger("sillytavern-session-proxy.hermes_ws")

# ─── Token retrieval from Hermes dashboard ───────────────────────────────────

_TOKEN_RE = re.compile(r'window\.__HERMES_SESSION_TOKEN__="([^"]+)"')
_WS_CREDENTIAL_RE = re.compile(r"([?&](?:token|ticket)=)[^&]+")
_THINKING_VERBS = (
    "pondering",
    "contemplating",
    "musing",
    "cogitating",
    "ruminating",
    "thinking",
    "processing",
    "analyzing",
    "computing",
    "synthesizing",
    "formulating",
    "brainstorming",
)
_THINKING_STATUS_SUFFIX = r"(?:…|\.{1,3})?"
_THINKING_STATUS_RE = re.compile(
    rf"^(?:{'|'.join(_THINKING_VERBS)}){_THINKING_STATUS_SUFFIX}$",
    re.IGNORECASE,
)
_THINKING_STATUS_CHUNK_RE = re.compile(
    rf"[^A-Za-z\n]+\s*(?:{'|'.join(_THINKING_VERBS)}){_THINKING_STATUS_SUFFIX}\s*",
    re.IGNORECASE,
)
_PERSONA_PATCH_EVENTS = {
    "persona.patch",
    "persona.update",
    "sillytavern.persona.patch",
    "sillytavern.persona.update",
}
_PROMPT_SUBMIT_STREAM_STATUS = "streaming"
_PROMPT_SUBMIT_ACCEPTED_STATUSES = frozenset({
    "streaming",
    "queued",
    "steered",
    "redirected",
})
_PROMPT_SUBMIT_NON_STREAM_MESSAGES = {
    "queued": "Hermes accepted the prompt as queued; no dedicated stream is attached. Do not retry.",
    "steered": "Hermes accepted the prompt as a steering correction; no dedicated stream is attached. Do not retry.",
    "redirected": "Hermes accepted the prompt as a redirected turn; no dedicated stream is attached. Do not retry.",
}

# ``prompt.submit`` starts the asynchronous Hermes turn only after returning a
# JSON-RPC acknowledgement.  Keep this separate from the much longer stream
# idle timeout: after this deadline the submission outcome is deliberately
# treated as unknown and is never retried.
PROMPT_SUBMIT_ACK_TIMEOUT_SECONDS = 5.0

_SERVER_REQUEST_METHODS = frozenset({"approval", "clarify", "sudo"})
_SERVER_REQUEST_ID_RE = re.compile(r"^srq-[A-Za-z0-9_-]{1,64}$")
_SERVER_REQUEST_MAX_TEXT = 4096
_SERVER_REQUEST_MAX_QUESTIONS = 64
_SERVER_REQUEST_MAX_CHOICES = 64
_SERVER_REQUEST_MAX_ANSWER = 4096
_APPROVAL_CHOICES = frozenset({"once", "session", "always", "deny"})


@dataclass
class ServerRequestRecord:
    """Ephemeral correlation data for one Hermes server→client request."""

    rpc_id: str
    st_session_id: str
    tui_session_id: str
    method: str
    frontend_params: dict[str, Any]

    def frontend_message(self) -> dict[str, Any]:
        return {
            "type": "server_request",
            "session_id": self.st_session_id,
            "rpc_id": self.rpc_id,
            "method": self.method,
            "params": dict(self.frontend_params),
        }


def _exception_summary(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return f"{exc.__class__.__name__}: {text}"
    return exc.__class__.__name__


def _json_rpc_error_text(error: Any) -> str:
    """Return a bounded, non-serialized JSON-RPC error for user-facing paths."""
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
    else:
        code = None
        message = None

    code_text = str(code).strip()[:64] if code is not None else ""
    message_text = " ".join(message.split())[:512] if isinstance(message, str) else ""
    if code_text and message_text:
        return f"Hermes error {code_text}: {message_text}"
    if message_text:
        return f"Hermes error: {message_text}"
    if code_text:
        return f"Hermes error {code_text}"
    return "Hermes returned an unspecified JSON-RPC error"


class HermesJsonRpcError(RuntimeError):
    """Bounded JSON-RPC error retained for internal classification.

    ``error.data`` is deliberately not copied: the gateway may put request
    context in it, and the proxy only needs the code and bounded message for
    routing and user-facing errors.
    """

    def __init__(self, code: Any, message: Any):
        self.code = code
        self.rpc_message = " ".join(message.split())[:512] if isinstance(message, str) else ""
        super().__init__(_json_rpc_error_text({"code": code, "message": self.rpc_message}))


class HermesSessionNotFoundError(RuntimeError):
    """Internal marker for the one safe prompt recovery path."""

    def __init__(self, cause: BaseException):
        self.safe_message = _safe_rpc_exception_text(cause)
        super().__init__(self.safe_message)


def _safe_rpc_exception_text(exc: BaseException) -> str:
    """Keep legacy RPC errors bounded without carrying their ``data`` field."""
    if isinstance(exc, HermesJsonRpcError):
        return str(exc)

    raw_text = str(exc)
    try:
        parsed = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError):
        return raw_text[:512]
    if isinstance(parsed, dict):
        envelope = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
        if isinstance(envelope, dict) and ("code" in envelope or "message" in envelope):
            return _json_rpc_error_text(envelope)
    return raw_text[:512]


def _append_ws_credential(ws_url: str, key: str, value: str) -> str:
    separator = "&" if "?" in ws_url else "?"
    return f"{ws_url}{separator}{key}={quote(value, safe='')}"


def _redact_ws_credentials(ws_url: str) -> str:
    return _WS_CREDENTIAL_RE.sub(r"\1***", ws_url)


def _clean_reasoning_text(text: str) -> str:
    """Strip Hermes spinner/status fragments while preserving stream spacing."""
    raw = str(text or "")
    if not raw:
        return ""
    cleaned_lines = []
    for original_line in raw.split("\n"):
        line = _THINKING_STATUS_CHUNK_RE.sub("", original_line)
        if not line.strip():
            if not original_line.strip():
                cleaned_lines.append(line)
        elif not _THINKING_STATUS_RE.match(line.strip()):
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _persona_patch_request_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    content = (
        payload.get("content")
        or payload.get("description")
        or payload.get("persona")
        or payload.get("text")
    )
    content = str(content or "").strip()
    if not content:
        return None
    return {
        "content": content,
        "summary": payload.get("summary") or payload.get("title") or "Agent proposed a persona update",
        "reason": payload.get("reason") or "",
        "request_id": payload.get("request_id") or payload.get("id"),
        "source": payload.get("source") or "hermes",
        "created_at": payload.get("created_at") or now_iso(),
    }


async def _fetch_session_token(dashboard_url: str, timeout: float = 5.0) -> Optional[str]:
    """Retrieve the session token from the Hermes dashboard."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(dashboard_url)
            if response.status_code == 200:
                match = _TOKEN_RE.search(response.text)
                if match:
                    token = match.group(1)
                    logger.info("Retrieved Hermes session token from dashboard")
                    return token
                logger.warning("Hermes session token not found in dashboard HTML")
            else:
                logger.warning("Hermes dashboard returned HTTP %d", response.status_code)
    except httpx.HTTPError as exc:
        logger.warning("Could not retrieve dashboard token: %s", _exception_summary(exc))
    except Exception:
        logger.exception("Error retrieving dashboard token")
    return None


class HermesSession:
    """Represent an active Hermes session on the WebSocket connection."""

    def __init__(self, tui_session_id: str, st_session_id: str):
        self.tui_session_id = tui_session_id
        self.st_session_id = st_session_id
        # request_id -> asyncio.Queue for request events
        self.pending_queues: dict[int, asyncio.Queue] = {}
        # Current request ID (prompt.submit)
        self.current_request_id: Optional[int] = None
        self.is_processing = False
        # Set while no Hermes turn is active. Unlike ``is_processing``, this
        # remains cleared if the HTTP/SSE consumer is cancelled and is only set
        # by Hermes' terminal event. This prevents a late interrupted event from
        # being attributed to the next prompt.
        self.turn_complete_event = asyncio.Event()
        self.turn_complete_event.set()
        self._request_counter = 0
        self.info: dict[str, Any] = {}
        self.tool_progress_mode_configured: str | None = None
        self.submitted_prompt_count = 0
        self.requested_cwd: str | None = None
        self.requested_profile: str | None = None
        self.requested_model: str | None = None
        self.requested_context_signature: str | None = None
        # A reconnect gives us a new transport, not necessarily a new Hermes
        # runtime. Validate old mappings lazily instead of rebuilding all of
        # them; the first successful probe clears this flag for the connection.
        self.mapping_needs_validation = False
        self.mapping_validation_task: asyncio.Task[bool] | None = None
        self.mapping_validation_usage: dict[str, Any] | None = None
        self._broadcast_tasks: set[asyncio.Task] = set()

    def next_request_id(self) -> int:
        self._request_counter += 1
        return self._request_counter


class HermesWebSocketManager:
    """
    Persistent WebSocket manager for the Hermes tui_gateway.

    Maintains one WebSocket connection, manages multiple Hermes sessions, and
    provides an async interface for submitting prompts and streaming responses.
    """

    def __init__(
        self,
        ws_url: str = "ws://localhost:8642/api/ws",
        dashboard_url: str = "",
        *,
        dashboard_auth_mode: str = "auto",
        dashboard_auth_provider: str = "basic",
        dashboard_auth_username: str = "",
        dashboard_auth_password: str = "",
        dashboard_auth_timeout: float = 5.0,
        dashboard_authenticator: HermesDashboardAuthenticator | None = None,
    ):
        self.ws_url = ws_url
        self.dashboard_url = dashboard_url
        self.dashboard_auth_mode = self._normalize_dashboard_auth_mode(dashboard_auth_mode)
        self.dashboard_auth = HermesDashboardPasswordAuth(
            provider=dashboard_auth_provider,
            username=dashboard_auth_username,
            password=dashboard_auth_password,
        )
        self.dashboard_auth_timeout = dashboard_auth_timeout
        self._dashboard_authenticator = dashboard_authenticator
        self._session_token: Optional[str] = None
        self._ws: Optional[Any] = None
        self._should_connect = False
        self._ready = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._reconnect_task: Optional[asyncio.Task] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._global_id = 0

        # session_tui_id -> HermesSession
        self._sessions: dict[str, HermesSession] = {}
        # st_session_id -> tui_session_id
        self._st_to_tui: dict[str, str] = {}
        # tui_session_id waiting for session.info
        self._pending_creations: dict[str, str] = {}  # pending_key -> st_session_id
        self._pending_requests: dict[int, str] = {}  # request_id -> st_session_id
        self._rpc_waiters: dict[int, asyncio.Future] = {}
        # Futures awaiting session creation confirmation
        self._creation_waiters: dict[str, asyncio.Future] = {}  # st_session_id -> Future
        # Native Hermes server requests are deliberately process-local. They
        # contain no raw JSON-RPC payload and are never passed to persistence.
        self._server_requests: dict[str, ServerRequestRecord] = {}
        self._server_request_locks: dict[str, asyncio.Lock] = {}
        self._server_request_restore_task: asyncio.Task | None = None

        # Callbacks for global events
        self._on_tool_call: list[Callable] = []
        self._on_reasoning: list[Callable] = []
        self._on_text: list[Callable] = []

        logger.info(
            "HermesWebSocketManager initialized url=%s dashboard=%s auth_mode=%s",
            ws_url,
            dashboard_url,
            self.dashboard_auth_mode,
        )

    @staticmethod
    def _normalize_dashboard_auth_mode(value: str) -> str:
        mode = (value or "auto").strip().lower()
        if mode not in {"auto", "password", "legacy", "none", "off"}:
            return "auto"
        return mode

    def _persist_session_info(self, session: HermesSession) -> None:
        info = SESSION_INFOS.setdefault(session.st_session_id, {"last_usage": None, "total_requests": 0})
        hermes_info = dict(getattr(session, "info", {}) or {})
        hermes_info["tui_session_id"] = session.tui_session_id
        if session.requested_cwd is not None:
            hermes_info.setdefault("cwd", session.requested_cwd)
        if session.requested_profile is not None:
            hermes_info.setdefault("profile_name", session.requested_profile)
        if session.requested_model is not None:
            hermes_info.setdefault("model", session.requested_model)
        info["hermes"] = hermes_info
        if hermes_info.get("model"):
            info["model"] = hermes_info.get("model")
        if hermes_info.get("reasoning_effort") is not None:
            info["reasoning_effort"] = hermes_info.get("reasoning_effort")
        info["updated_at"] = now_iso()
        _save_sessions()

    def _clear_disallowed_persisted_profile(self, info: dict[str, Any]) -> None:
        """Remove revoked profile labels before session restoration or reporting."""
        if profile_allowlist() is None:
            return
        hermes_value = info.get("hermes")
        hermes_info = dict(hermes_value) if isinstance(hermes_value, dict) else {}
        changed = False
        hermes_profile = hermes_info.get("profile_name")
        if hermes_profile is not None and not is_profile_allowed(hermes_profile):
            hermes_info.pop("profile_name", None)
            changed = True
        stored_profile = info.get("profile")
        if stored_profile is not None and not is_profile_allowed(stored_profile):
            info.pop("profile", None)
            changed = True
        if not changed:
            return
        if isinstance(hermes_value, dict):
            if hermes_info:
                info["hermes"] = hermes_info
            else:
                info.pop("hermes", None)
        info["updated_at"] = now_iso()
        _save_sessions()

    # ─── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """Start the WebSocket connection and listener task."""
        if self._should_connect:
            return
        self._should_connect = True  # Mark as wanting to connect
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        logger.info("HermesWebSocketManager started")

    async def stop(self) -> None:
        """Stop the WebSocket connection cleanly."""
        self._should_connect = False
        await self._cancel_server_requests(reason="Hermes connection lost")
        if self._server_request_restore_task:
            self._server_request_restore_task.cancel()
            self._server_request_restore_task = None
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._dashboard_authenticator:
            await self._dashboard_authenticator.close()
            self._dashboard_authenticator = None
        logger.info("HermesWebSocketManager stopped")

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential backoff."""
        delay = self._reconnect_delay
        while self._should_connect:
            try:
                await self._do_connect_and_wait()
                delay = self._reconnect_delay  # Reset on success
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._should_connect:  # Only log if we're still trying
                    logger.warning("Hermes connection failed: %s — retrying in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _fetch_ws_ticket(self) -> str:
        if self._dashboard_authenticator is None:
            self._dashboard_authenticator = HermesDashboardAuthenticator(
                self.dashboard_url,
                auth=self.dashboard_auth,
                timeout=self.dashboard_auth_timeout,
            )
        return await self._dashboard_authenticator.fetch_ws_ticket()

    async def _authenticated_ws_url(self) -> str:
        ws_url = self.ws_url
        if not self.dashboard_url or self.dashboard_auth_mode in {"none", "off"}:
            return ws_url

        if self.dashboard_auth_mode == "password" or (
            self.dashboard_auth_mode == "auto" and self.dashboard_auth.is_configured
        ):
            try:
                ticket = await self._fetch_ws_ticket()
            except DashboardAuthError as exc:
                if self.dashboard_auth_mode == "password":
                    raise ConnectionError(f"Unable to obtain a Hermes WebSocket ticket: {exc}") from exc
                logger.warning(
                    "Hermes WebSocket ticket unavailable (%s); falling back to legacy HTML token",
                    exc,
                )
            else:
                self._session_token = None
                return _append_ws_credential(ws_url, "ticket", ticket)

        if self.dashboard_auth_mode in {"auto", "legacy"}:
            # Legacy loopback dashboards inject this token in the SPA HTML.
            self._session_token = await _fetch_session_token(self.dashboard_url)
            if self._session_token:
                return _append_ws_credential(ws_url, "token", self._session_token)

        raise ConnectionError("Could not retrieve a Hermes WebSocket token or ticket from the dashboard")

    async def _do_connect(self) -> None:
        """Establish the WebSocket connection and start its listener."""
        ws_url = await self._authenticated_ws_url()

        safe_ws_url = _redact_ws_credentials(ws_url)
        logger.info("Connecting to Hermes tui_gateway: %s", safe_ws_url)
        self._ws = await websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=20,
        )
        self._ready = False
        logger.info("Hermes WebSocket connection established")
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _do_connect_and_wait(self) -> None:
        """Connect, then block until the listen loop finishes (connection drops)."""
        await self._do_connect()
        if self._listen_task:
            await self._listen_task

    def _cleanup_prompt_request(
        self,
        session: HermesSession,
        request_id: int,
        *,
        queue_error: str | None = None,
    ) -> None:
        """Idempotently release one prompt's queue, waiter, and active-turn state."""
        queue = session.pending_queues.pop(request_id, None)
        if queue_error and queue is not None:
            try:
                queue.put_nowait({"type": "error", "message": queue_error})
            except asyncio.QueueFull:
                pass
        waiter = self._rpc_waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.cancel()
        if session.current_request_id == request_id:
            session.current_request_id = None
            session.is_processing = False
            session.turn_complete_event.set()

    async def _fail_pending_operations_on_disconnect(self) -> None:
        """Wake all RPC consumers and terminate active prompt streams on transport loss."""
        connection_error = ConnectionError("Hermes WebSocket connection closed")
        if self._server_request_restore_task and self._server_request_restore_task is not asyncio.current_task():
            self._server_request_restore_task.cancel()
            self._server_request_restore_task = None
        await self._cancel_server_requests(reason="Hermes connection lost")
        waiters = list(self._rpc_waiters.items())
        self._rpc_waiters.clear()
        for _request_id, waiter in waiters:
            if not waiter.done():
                waiter.set_exception(connection_error)

        for session in list(self._sessions.values()):
            for request_id in list(session.pending_queues):
                self._cleanup_prompt_request(
                    session,
                    request_id,
                    queue_error="Hermes connection lost while processing the prompt",
                )
            session.current_request_id = None
            session.is_processing = False
            session.turn_complete_event.set()

        pending_creation_sessions = set(self._pending_creations.values())
        pending_creation_sessions.update(self._pending_requests.values())
        pending_creation_sessions.update(self._creation_waiters)
        for st_session_id in pending_creation_sessions:
            self._cleanup_pending_creation(st_session_id)
        for pending_key in list(self._sessions):
            if pending_key.startswith("__pending__"):
                self._sessions.pop(pending_key, None)
        for st_session_id, tui_session_id in list(self._st_to_tui.items()):
            if tui_session_id.startswith("__pending__"):
                self._st_to_tui.pop(st_session_id, None)
        self._pending_requests.clear()
        self._pending_creations.clear()
        for st_session_id, waiter in list(self._creation_waiters.items()):
            if not waiter.done():
                waiter.set_exception(connection_error)
            self._creation_waiters.pop(st_session_id, None)

    async def _listen_loop(self) -> None:
        """Lit les messages entrants et les dispatche."""
        try:
            async for raw_message in self._ws:
                try:
                    await self._handle_message(raw_message)
                except (RuntimeError, asyncio.TimeoutError, json.JSONDecodeError):
                    logger.exception("Error processing Hermes message")
                except Exception as e:
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    logger.exception("Unexpected error processing Hermes message")
        except asyncio.CancelledError:
            return
        except (OSError, ConnectionError, RuntimeError):
            logger.exception("Hermes WebSocket connection error")
        except Exception as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.exception("Unexpected Hermes WebSocket error")
        finally:
            await self._fail_pending_operations_on_disconnect()
            ws = self._ws
            self._ready = False
            if self._ws is ws:
                self._ws = None

    @staticmethod
    def _bounded_text(value: Any, limit: int = _SERVER_REQUEST_MAX_TEXT) -> str | None:
        if not isinstance(value, str) or len(value) > limit:
            return None
        return value

    @classmethod
    def _frontend_server_request_params(
        cls,
        method: str,
        params: Any,
    ) -> dict[str, Any] | None:
        """Copy only renderer-safe fields from a Hermes request.

        In particular, the TUI session id and reconnect-only private answers
        never cross the browser boundary or enter the local registry.
        """
        if not isinstance(params, dict):
            return None

        if method == "approval":
            request_id = cls._bounded_text(params.get("request_id"), 256)
            if not request_id:
                return None
            command = params.get("command", "")
            description = params.get("description", "")
            if not isinstance(command, str) or len(command) > _SERVER_REQUEST_MAX_TEXT:
                return None
            if not isinstance(description, str) or len(description) > _SERVER_REQUEST_MAX_TEXT:
                return None
            raw_choices = params.get("choices")
            if raw_choices is None:
                approval_choices = ["once", "session", "always", "deny"]
            elif (
                not isinstance(raw_choices, list)
                or len(raw_choices) > _SERVER_REQUEST_MAX_CHOICES
                or any(choice not in _APPROVAL_CHOICES for choice in raw_choices)
            ):
                return None
            else:
                approval_choices = list(dict.fromkeys(raw_choices))
            result: dict[str, Any] = {
                "request_id": request_id,
                "command": command,
                "description": description,
                "choices": approval_choices,
            }
            for key in ("allow_permanent", "allow_session", "smart_denied"):
                if key in params:
                    if not isinstance(params[key], bool):
                        return None
                    result[key] = params[key]
            tool_name = params.get("tool_name")
            if tool_name is not None:
                tool_name = cls._bounded_text(tool_name, 256)
                if tool_name is None:
                    return None
                result["tool_name"] = tool_name
            return result

        if method == "clarify":
            questions = params.get("questions")
            if questions is not None:
                if not isinstance(questions, list) or not questions or len(questions) > _SERVER_REQUEST_MAX_QUESTIONS:
                    return None
                safe_questions: list[dict[str, Any]] = []
                for question in questions:
                    if not isinstance(question, dict):
                        return None
                    qid = cls._bounded_text(question.get("qid"), 256)
                    prompt = cls._bounded_text(question.get("question"))
                    if not qid or prompt is None:
                        return None
                    safe_question: dict[str, Any] = {"qid": qid, "question": prompt}
                    question_choices: Any = question.get("choices")
                    if question_choices is not None:
                        if (
                            not isinstance(question_choices, list)
                            or len(question_choices) > _SERVER_REQUEST_MAX_CHOICES
                            or any(
                                not isinstance(choice, str) or len(choice) > _SERVER_REQUEST_MAX_TEXT
                                for choice in question_choices
                            )
                        ):
                            return None
                        safe_question["choices"] = list(question_choices)
                    multi_select = question.get("multi_select", False)
                    if not isinstance(multi_select, bool):
                        return None
                    safe_question["multi_select"] = multi_select
                    safe_questions.append(safe_question)
                locked_answers = params.get("answers")
                if isinstance(locked_answers, dict):
                    locked_qids = {
                        qid for qid in locked_answers
                        if isinstance(qid, str) and len(qid) <= 256
                    }
                    safe_questions = [
                        question for question in safe_questions
                        if question["qid"] not in locked_qids
                    ]
                    if not safe_questions:
                        return None
                return {"questions": safe_questions}

            question = cls._bounded_text(params.get("question"))
            if question is None:
                return None
            result = {"question": question}
            clarify_choices: Any = params.get("choices")
            if clarify_choices is not None:
                if (
                    not isinstance(clarify_choices, list)
                    or len(clarify_choices) > _SERVER_REQUEST_MAX_CHOICES
                    or any(not isinstance(choice, str) or len(choice) > _SERVER_REQUEST_MAX_TEXT for choice in clarify_choices)
                ):
                    return None
                result["choices"] = list(clarify_choices)
            multi_select = params.get("multi_select", False)
            if not isinstance(multi_select, bool):
                return None
            result["multi_select"] = multi_select
            return result

        if method == "sudo":
            # Hermes currently sends no renderer-visible fields for sudo. Do
            # not copy future fields until they have an explicit allowlist.
            return {}

        return None

    @staticmethod
    def _valid_server_request_id(value: Any) -> bool:
        return isinstance(value, str) and bool(_SERVER_REQUEST_ID_RE.fullmatch(value))

    async def _send_server_request_error(self, rpc_id: str, message: str, code: int = -32601) -> None:
        """Reject a native request without echoing its params."""
        if not self._ws or not self.is_connected:
            return
        frame = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            # The request is not replayed: transport ambiguity must not cause
            # an approval or secret-like prompt to be answered twice.
            logger.debug("Unable to reject Hermes server request id=%s", rpc_id)

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        rpc_id_value = msg.get("id")
        method_value = msg.get("method")
        if not isinstance(rpc_id_value, str) or not self._valid_server_request_id(rpc_id_value):
            return
        if not isinstance(method_value, str) or method_value not in _SERVER_REQUEST_METHODS:
            await self._send_server_request_error(rpc_id_value, "Method not supported")
            return
        rpc_id = rpc_id_value
        method = method_value

        params = msg.get("params")
        tui_session_id = params.get("session_id") if isinstance(params, dict) else None
        if not isinstance(tui_session_id, str):
            await self._send_server_request_error(rpc_id, "Unknown session", -32602)
            return
        session = self._find_session_by_tui(tui_session_id)
        if not session:
            await self._send_server_request_error(rpc_id, "Unknown session", -32602)
            return
        if not self._session_profile_is_allowed(session):
            await self._send_server_request_error(rpc_id, "Session is no longer allowed", -32603)
            await self.close_session(session.st_session_id)
            return
        frontend_params = self._frontend_server_request_params(method, params)
        if frontend_params is None:
            await self._send_server_request_error(rpc_id, "Invalid params", -32602)
            return

        record = ServerRequestRecord(
            rpc_id=rpc_id,
            st_session_id=session.st_session_id,
            tui_session_id=session.tui_session_id,
            method=method,
            frontend_params=frontend_params,
        )
        existing = self._server_requests.get(rpc_id)
        if existing is not None:
            if (
                existing.st_session_id == record.st_session_id
                and existing.tui_session_id == record.tui_session_id
                and existing.method == record.method
                and existing.frontend_params == record.frontend_params
            ):
                return
            await self._send_server_request_error(rpc_id, "Duplicate request id", -32600)
            return

        self._server_requests[rpc_id] = record
        self._server_request_locks.setdefault(rpc_id, asyncio.Lock())
        await ws_broadcast(session.st_session_id, record.frontend_message())

    async def _cancel_server_requests(
        self,
        st_session_id: str | None = None,
        *,
        tui_session_id: str | None = None,
        reason: str = "Hermes request cancelled",
    ) -> None:
        records = [
            record
            for record in self._server_requests.values()
            if (st_session_id is None or record.st_session_id == st_session_id)
            and (tui_session_id is None or record.tui_session_id == tui_session_id)
        ]
        for record in records:
            if self._server_requests.pop(record.rpc_id, None) is None:
                continue
            self._server_request_locks.pop(record.rpc_id, None)
            await ws_broadcast(
                record.st_session_id,
                {
                    "type": "server_request_cancel",
                    "session_id": record.st_session_id,
                    "rpc_id": record.rpc_id,
                    "method": record.method,
                    "reason": "Hermes request cancelled" if reason else "Hermes request cancelled",
                },
            )

    async def _cancel_server_request(self, record: ServerRequestRecord) -> None:
        """Cancel exactly one request, identified by its current record."""
        if self._server_requests.pop(record.rpc_id, None) is not record:
            return
        self._server_request_locks.pop(record.rpc_id, None)
        await ws_broadcast(
            record.st_session_id,
            {
                "type": "server_request_cancel",
                "session_id": record.st_session_id,
                "rpc_id": record.rpc_id,
                "method": record.method,
                "reason": "Hermes request cancelled",
            },
        )

    async def _broadcast_server_request_error(
        self,
        record: ServerRequestRecord,
        status: str,
    ) -> None:
        """Notify browsers with correlation metadata only."""
        await ws_broadcast(
            record.st_session_id,
            {
                "type": "server_request_error",
                "session_id": record.st_session_id,
                "rpc_id": record.rpc_id,
                "method": record.method,
                "status": status,
            },
        )

    def _remove_server_request(self, record: ServerRequestRecord) -> bool:
        """Remove a request only if the registry still points at *record*."""
        if self._server_requests.pop(record.rpc_id, None) is not record:
            return False
        self._server_request_locks.pop(record.rpc_id, None)
        return True

    async def _mark_server_request_delivery_uncertain(self, record: ServerRequestRecord) -> None:
        if self._remove_server_request(record):
            await self._broadcast_server_request_error(record, "delivery_uncertain")

    async def _handle_request_cancel(self, session: HermesSession, params: dict[str, Any]) -> None:
        event_session_id = params.get("session_id")
        if isinstance(event_session_id, str) and event_session_id != session.tui_session_id:
            return
        raw_payload = params.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        rpc_id = payload.get("id")
        method = payload.get("method")
        if (
            not isinstance(rpc_id, str)
            or not self._valid_server_request_id(rpc_id)
            or not isinstance(method, str)
            or method not in _SERVER_REQUEST_METHODS
        ):
            return
        record = self._server_requests.get(rpc_id)
        if not record or record.tui_session_id != session.tui_session_id or record.method != method:
            return
        await self._cancel_server_request(record)

    def server_request_snapshot(self, st_session_id: str) -> list[dict[str, Any]]:
        return [
            record.frontend_message()
            for record in self._server_requests.values()
            if record.st_session_id == st_session_id
        ]

    def _server_request_record_for_socket(
        self,
        ws: Any,
        session_id: Any,
        rpc_id: Any,
        method: Any,
    ) -> ServerRequestRecord:
        if not isinstance(session_id, str) or not self._valid_server_request_id(rpc_id):
            raise ValueError("Invalid server request identity")
        if method not in _SERVER_REQUEST_METHODS:
            raise ValueError("Unsupported server request")
        if not ws_is_subscribed(ws, session_id):
            raise ValueError("WebSocket is not subscribed to this session")
        record = self._server_requests.get(rpc_id)
        if not record or record.st_session_id != session_id or record.method != method:
            raise ValueError("Unknown or foreign server request")
        if self._st_to_tui.get(session_id) != record.tui_session_id:
            raise ValueError("Server request session mapping changed")
        return record

    def _session_profile_is_allowed(self, session: HermesSession) -> bool:
        """Fail closed on unknown or revoked live profile labels in restricted mode."""
        if profile_allowlist() is None:
            return True
        session_info = getattr(session, "info", {}) or {}
        profile = session_info.get("profile_name") if isinstance(session_info, dict) else None
        profile = profile or session.requested_profile
        return is_profile_allowed(profile)

    def _require_allowed_session_profile(self, session: HermesSession) -> None:
        if not self._session_profile_is_allowed(session):
            raise ProfileNotAllowedError()

    @classmethod
    def _validate_server_request_result(
        cls,
        record: ServerRequestRecord,
        result: Any,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("Server request result must be an object")
        if record.method == "approval":
            if set(result) - {"choice", "all"} or result.get("choice") not in _APPROVAL_CHOICES:
                raise ValueError("Invalid approval result")
            if "choices" in record.frontend_params and result["choice"] not in record.frontend_params["choices"]:
                raise ValueError("Approval choice is not offered")
            if "all" in result and not isinstance(result["all"], bool):
                raise ValueError("Invalid approval result")
            return {"choice": result["choice"], **({"all": result["all"]} if "all" in result else {})}
        if record.method == "clarify":
            if set(result) != {"answer"} or not isinstance(result.get("answer"), str):
                raise ValueError("Invalid clarify result")
            if len(result["answer"]) > _SERVER_REQUEST_MAX_ANSWER:
                raise ValueError("Clarify answer is too long")
            if "questions" in record.frontend_params:
                raise ValueError("Batch clarify requires clarify.lock")
            return {"answer": result["answer"]}
        if record.method == "sudo":
            if set(result) != {"value"} or not isinstance(result.get("value"), str):
                raise ValueError("Invalid sudo result")
            if len(result["value"]) > _SERVER_REQUEST_MAX_ANSWER:
                raise ValueError("Sudo value is too long")
            return {"value": result["value"]}
        raise ValueError("Unsupported server request")

    async def respond_server_request(
        self,
        ws: Any,
        session_id: Any,
        rpc_id: Any,
        method: Any,
        result: Any,
    ) -> dict[str, Any]:
        record = self._server_request_record_for_socket(ws, session_id, rpc_id, method)
        session = self.get_session(record.st_session_id)
        if session is None or not self._session_profile_is_allowed(session):
            await self.close_session(record.st_session_id)
            return {"status": "rejected", "rpc_id": record.rpc_id, "method": record.method}
        try:
            safe_result = self._validate_server_request_result(record, result)
        except ValueError:
            await self._broadcast_server_request_error(record, "rejected")
            return {"status": "rejected", "rpc_id": record.rpc_id, "method": record.method}
        # Pop before touching the Hermes socket. If the send becomes
        # ambiguous, a second browser response is still rejected.
        self._remove_server_request(record)
        try:
            await self._send_server_request_response(record.rpc_id, safe_result)
        except Exception:
            await self._broadcast_server_request_error(record, "delivery_uncertain")
            return {"status": "delivery_uncertain", "rpc_id": record.rpc_id, "method": record.method}
        await ws_broadcast(
            record.st_session_id,
            {
                "type": "server_request_resolved",
                "session_id": record.st_session_id,
                "rpc_id": record.rpc_id,
                "method": record.method,
            },
        )
        return {"status": "ok", "rpc_id": record.rpc_id, "method": record.method}

    async def lock_server_request(
        self,
        ws: Any,
        session_id: Any,
        rpc_id: Any,
        question_id: Any,
        answer: Any,
    ) -> dict[str, Any]:
        if not self._valid_server_request_id(rpc_id):
            raise ValueError("Invalid server request identity")
        lock = self._server_request_locks.get(rpc_id)
        if lock is None:
            raise ValueError("Unknown or expired server request")
        async with lock:
            record = self._server_request_record_for_socket(ws, session_id, rpc_id, "clarify")
            session = self.get_session(record.st_session_id)
            if session is None or not self._session_profile_is_allowed(session):
                await self.close_session(record.st_session_id)
                return {"status": "rejected", "remaining": []}
            questions = record.frontend_params.get("questions")
            try:
                if not isinstance(questions, list):
                    raise ValueError("Clarify lock requires a batch request")
                question_id = self._bounded_text(question_id, 256)
                answer = self._bounded_text(answer, _SERVER_REQUEST_MAX_ANSWER)
                if not question_id or answer is None:
                    raise ValueError("Invalid clarify lock")
                if question_id not in {question["qid"] for question in questions}:
                    raise ValueError("Unknown clarify question")
            except ValueError:
                await self._broadcast_server_request_error(record, "rejected")
                remaining = [question["qid"] for question in questions] if isinstance(questions, list) else []
                return {"status": "rejected", "remaining": remaining}

            try:
                result = await self._request_json_rpc(
                    "clarify.lock",
                    {"request_id": record.rpc_id, "question_id": question_id, "answer": answer},
                    timeout=15.0,
                )
            except HermesJsonRpcError:
                # Hermes explicitly rejected the answer; the question is still
                # open and can be submitted again.
                if self._server_requests.get(record.rpc_id) is record:
                    await self._broadcast_server_request_error(record, "rejected")
                    return {"status": "rejected", "remaining": [question["qid"] for question in questions]}
                return {"status": "expired", "remaining": []}
            except Exception:
                await self._mark_server_request_delivery_uncertain(record)
                return {"status": "delivery_uncertain", "remaining": []}
            if self._server_requests.get(record.rpc_id) is not record:
                return {"status": "expired", "remaining": []}
            remaining = result.get("remaining") if isinstance(result, dict) else None
            if not isinstance(remaining, list) or any(not isinstance(item, str) for item in remaining):
                await self._mark_server_request_delivery_uncertain(record)
                return {"status": "delivery_uncertain", "remaining": []}
            remaining_set = set(remaining)
            if not remaining_set.issubset({question["qid"] for question in questions}):
                await self._mark_server_request_delivery_uncertain(record)
                return {"status": "delivery_uncertain", "remaining": []}
            if remaining:
                record.frontend_params["questions"] = [
                    question for question in questions if question["qid"] in remaining_set
                ]
                await ws_broadcast(record.st_session_id, record.frontend_message())
            else:
                self._remove_server_request(record)
                await ws_broadcast(
                    record.st_session_id,
                    {
                        "type": "server_request_resolved",
                        "session_id": record.st_session_id,
                        "rpc_id": record.rpc_id,
                        "method": record.method,
                    },
                )
            return {"status": "ok", "remaining": remaining}

    async def _send_server_request_response(self, rpc_id: str, result: dict[str, Any]) -> None:
        if not self._ws or not self.is_connected:
            raise ConnectionError("Hermes WebSocket connection closed")
        frame = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _handle_message(self, raw: str) -> None:
        """Parse and dispatch an incoming JSON-RPC message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON message from Hermes: %s", raw[:200])
            return

        method = msg.get("method")

        # Event
        if method == "event":
            params = msg.get("params", {})
            event_type = params.get("type", "")
            session_id = params.get("session_id")

            # gateway.ready
            if event_type == "gateway.ready":
                skin = params.get("payload", {}).get("skin", {})
                skin_name = skin.get("name", "unknown") if isinstance(skin, dict) else str(skin)
                self._ready = True
                for existing_session in self._sessions.values():
                    if not existing_session.tui_session_id.startswith("__pending__"):
                        existing_session.mapping_needs_validation = True
                        existing_session.mapping_validation_usage = None
                logger.info("Hermes gateway ready — skin=%s", skin_name)
                # Force Hermes to discover our MCP server now that we are connected
                asyncio.create_task(self._request_json_rpc("reload.mcp", {"confirm": True}))
                self._schedule_server_request_restore()
                return

            # Find the corresponding session
            session = self._find_session_by_tui(session_id) if session_id else None
            if not session:
                # Check whether this is a session awaiting creation
                if event_type == "session.info" and session_id:
                    await self._handle_session_info(session_id, params)
                return

            await self._dispatch_event(session, event_type, params)

        # Native Hermes server->client requests have both ``method`` and
        # ``id``. They must never reach the proxy-response waiter table.
        elif "method" in msg and "id" in msg:
            await self._handle_server_request(msg)

        # Response to a JSON-RPC request initiated by the proxy
        elif "id" in msg:
            await self._handle_response(msg)

    def _schedule_server_request_restore(self) -> None:
        task = self._server_request_restore_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._restore_server_requests())
        self._server_request_restore_task = task

        def _restore_done(done: asyncio.Task) -> None:
            if self._server_request_restore_task is done:
                self._server_request_restore_task = None
            if not done.cancelled():
                try:
                    done.result()
                except Exception:
                    logger.exception("Hermes server request restoration failed")

        task.add_done_callback(_restore_done)

    async def _restore_server_requests(self) -> None:
        """Recover only Hermes' open interactive requests after gateway.ready."""
        mappings = [
            (session.st_session_id, session.tui_session_id)
            for session in self._sessions.values()
            if not session.tui_session_id.startswith("__pending__")
        ]
        for st_session_id, tui_session_id in mappings:
            if not self.is_connected or not self.is_ready:
                return
            try:
                result = await self._request_json_rpc(
                    "session.events.since",
                    {"session_id": tui_session_id, "last_seen": 0},
                    timeout=5.0,
                )
            except (RuntimeError, asyncio.TimeoutError, ConnectionError):
                continue
            open_requests = result.get("open_requests") if isinstance(result, dict) else None
            if not isinstance(open_requests, list):
                continue
            for entry in open_requests:
                if not isinstance(entry, dict):
                    continue
                params = entry.get("params")
                if not isinstance(params, dict):
                    continue
                # ``open_requests`` entries are the same native request shape
                # without a JSON-RPC envelope. Ordinary replay events are
                # intentionally ignored in this recovery path.
                await self._handle_server_request({
                    "jsonrpc": "2.0",
                    "id": entry.get("id"),
                    "method": entry.get("method"),
                    "params": params,
                })

    async def _handle_session_info(self, tui_id: str, params: dict[str, Any]) -> None:
        """Handle session.info to complete session creation."""
        for pending_key, st_session_id in list(self._pending_creations.items()):
            session = self._sessions.get(pending_key)
            if session and session.tui_session_id == pending_key:
                payload = params.get("payload")
                if isinstance(payload, dict):
                    session.info = payload
                session.tui_session_id = tui_id
                self._sessions[tui_id] = session
                del self._sessions[pending_key]
                self._st_to_tui[st_session_id] = tui_id
                self._pending_creations.pop(pending_key, None)
                logger.info("Hermes session finalized ST=%s TUI=%s", st_session_id, tui_id)
                self._persist_session_info(session)
                # Resolve creation waiter
                creation_waiter = self._creation_waiters.pop(st_session_id, None)
                if creation_waiter and not creation_waiter.done():
                    creation_waiter.set_result(tui_id)
                return

    async def _handle_response(self, msg: dict[str, Any]) -> None:
        """Handle a JSON-RPC response (not an event)."""
        req_id = msg.get("id")
        if "error" in msg:
            waiter = self._rpc_waiters.pop(req_id, None)
            error = msg.get("error", {})
            rpc_error = HermesJsonRpcError(
                error.get("code") if isinstance(error, dict) else None,
                error.get("message") if isinstance(error, dict) else None,
            )
            if waiter and not waiter.done():
                waiter.set_exception(rpc_error)
            # Fail any pending session creation for this request
            st_session_id = self._pending_requests.pop(req_id, None)
            if st_session_id:
                self._cleanup_pending_creation(st_session_id)
                creation_waiter = self._creation_waiters.pop(st_session_id, None)
                if creation_waiter and not creation_waiter.done():
                    creation_waiter.set_exception(rpc_error)
            logger.error(
                "Hermes JSON-RPC error: id=%s code=%s",
                req_id,
                error.get("code") if isinstance(error, dict) else None,
            )
            return

        result = msg.get("result", {})

        # Response to session.create: {session_id: "<tui-sid>", info: {...}}
        if isinstance(result, dict) and "session_id" in result and "info" in result:
            tui_id = result["session_id"]
            # Find the pending session using the request_id
            st_session_id = self._pending_requests.pop(req_id, None)
            if not st_session_id:
                logger.warning("session.create response has no matching pending session req_id=%s", req_id)
                return
            # Find the pending_key for this st_session_id
            pending_key = next((k for k, v in self._pending_creations.items() if v == st_session_id), None)
            if not pending_key:
                logger.warning("Pending session not found for ST=%s", st_session_id)
                return
            session = self._sessions.get(pending_key)
            if not session:
                logger.warning("Session not found for pending_key=%s", pending_key)
                return
            if isinstance(result.get("info"), dict):
                session.info = result["info"]
            session.tui_session_id = tui_id
            del self._sessions[pending_key]
            self._sessions[tui_id] = session
            self._st_to_tui[st_session_id] = tui_id
            self._pending_creations.pop(pending_key, None)
            logger.info("Hermes session created ST=%s TUI=%s", st_session_id, tui_id)
            self._persist_session_info(session)
            # Resolve creation waiter
            creation_waiter = self._creation_waiters.pop(st_session_id, None)
            if creation_waiter and not creation_waiter.done():
                creation_waiter.set_result(tui_id)
            waiter = self._rpc_waiters.pop(req_id, None)
            if waiter and not waiter.done():
                waiter.set_result(result)
            return

        waiter = self._rpc_waiters.pop(req_id, None)
        if waiter and not waiter.done():
            waiter.set_result(result)

    async def _dispatch_event(
        self,
        session: HermesSession,
        event_type: str,
        params: dict[str, Any],
    ) -> None:
        """Dispatch an event to the queue for the current request."""
        if event_type == "request.cancel":
            await self._handle_request_cancel(session, params)
            return

        request_id = session.current_request_id
        queue = session.pending_queues.get(request_id) if request_id else None

        payload = params.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        text = payload.get("text", "")

        if event_type == "session.info" and isinstance(payload, dict):
            session.info = payload
            self._persist_session_info(session)
            return

        if event_type in _PERSONA_PATCH_EVENTS:
            patch_request = _persona_patch_request_from_payload(payload)
            if patch_request:
                task = asyncio.create_task(
                    ws_broadcast(
                        session.st_session_id,
                        {
                            "type": "persona_patch_request",
                            "session_id": session.st_session_id,
                            **patch_request,
                        },
                    )
                )
                session._broadcast_tasks.add(task)
                task.add_done_callback(session._broadcast_tasks.discard)
            return

        # Response stream events
        if event_type == "message.delta" and text and queue:
            for cb in self._on_text:
                await cb(session.st_session_id, text)
            try:
                queue.put_nowait({"type": "message.delta", "text": text})
            except asyncio.QueueFull:
                pass

        elif event_type in ("thinking.delta", "reasoning.delta") and text and queue:
            for cb in self._on_reasoning:
                await cb(session.st_session_id, text)
            try:
                queue.put_nowait({"type": event_type, "text": text})
            except asyncio.QueueFull:
                pass

        elif event_type == "tool.start" and queue:
            tool_info = {
                "tool_id": payload.get("tool_id"),
                "name": payload.get("name", ""),
                "context": payload.get("context", ""),
            }
            if payload.get("args_text"):
                tool_info["args_text"] = payload.get("args_text")
            if payload.get("todos"):
                tool_info["todos"] = payload.get("todos")
            try:
                queue.put_nowait({"type": "tool.start", "tool": tool_info})
            except asyncio.QueueFull:
                pass
            for cb in self._on_tool_call:
                await cb(session.st_session_id, "running", tool_info)

        elif event_type == "tool.complete" and queue:
            tool_info = {
                "tool_id": payload.get("tool_id"),
                "name": payload.get("name", ""),
                "duration_s": payload.get("duration_s"),
                "summary": payload.get("summary", ""),
            }
            for key in ("result_text", "stdout", "stderr", "exit_code", "returncode", "inline_diff", "error", "todos"):
                if payload.get(key):
                    tool_info[key] = payload.get(key)
            try:
                queue.put_nowait({"type": "tool.complete", "tool": tool_info})
            except asyncio.QueueFull:
                pass
            for cb in self._on_tool_call:
                await cb(session.st_session_id, "completed", tool_info)

        elif event_type == "message.complete":
            if queue:
                try:
                    queue.put_nowait({"type": "message.complete", "payload": payload})
                except asyncio.QueueFull:
                    pass
            session.current_request_id = None
            session.is_processing = False
            session.turn_complete_event.set()

        elif event_type == "error":
            error_msg = payload.get("message", "Unknown error")
            if queue:
                try:
                    queue.put_nowait({"type": "error", "message": error_msg})
                except asyncio.QueueFull:
                    pass
            session.current_request_id = None
            session.is_processing = False
            session.turn_complete_event.set()

        elif event_type == "status.update":
            if queue:
                try:
                    queue.put_nowait({"type": "status.update", "payload": payload})
                except asyncio.QueueFull:
                    pass

    # ─── Session management ──────────────────────────────────

    async def ensure_session(
        self,
        st_session_id: str,
        *,
        cwd: str | None = None,
        profile: str | None = None,
        model: str | None = None,
        messages: list[dict[str, str]] | None = None,
        system_context: str | None = None,
        persona_context: str | None = None,
        persona_reminder: str | None = None,
        persona_version: str | None = None,
    ) -> Optional[str]:
        """
        Ensure a Hermes session exists for the SillyTavern session ID.

        Create the session if needed and wait for confirmation.

        Returns:
            The tui_session_id, or None if creation fails.
        """
        restricted_profiles = profile_allowlist() is not None
        profile_was_explicit = profile is not None and (
            not isinstance(profile, str) or bool(profile.strip())
        )
        profile = resolve_profile_selection(profile)

        # Check whether a session already exists
        stable_context_supplied = any(
            value is not None
            for value in (system_context, persona_context, persona_reminder, persona_version)
        )
        context_signature = None
        if stable_context_supplied:
            context_signature = json.dumps(
                {
                    "system_context": system_context or "",
                    "persona_context": persona_context or "",
                    "persona_reminder": persona_reminder or "",
                    "persona_version": persona_version or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        persisted_info = SESSION_INFOS.get(st_session_id) or {}
        self._clear_disallowed_persisted_profile(persisted_info)
        persisted_hermes_value = persisted_info.get("hermes")
        persisted_hermes: dict[str, Any] = persisted_hermes_value if isinstance(persisted_hermes_value, dict) else {}
        if restricted_profiles and profile is None and not profile_was_explicit:
            persisted_profile = persisted_hermes.get("profile_name")
            if persisted_profile is None:
                persisted_profile = persisted_info.get("profile")
            profile = stored_profile_selection(persisted_profile)
        if st_session_id in self._st_to_tui:
            tui_id = self._st_to_tui[st_session_id]
            if tui_id in self._sessions:
                existing = self._sessions[tui_id]
                fallback_cwd = cwd if cwd is not None else existing.requested_cwd or persisted_hermes.get("cwd")
                existing_info = getattr(existing, "info", {}) or {}
                existing_profile_value = (
                    existing_info.get("profile_name")
                    or existing.requested_profile
                    or persisted_hermes.get("profile_name")
                    or persisted_info.get("profile")
                )
                existing_profile_disallowed = (
                    restricted_profiles
                    and not is_profile_allowed(existing_profile_value)
                )
                if restricted_profiles:
                    fallback_profile = (
                        profile
                        if profile_was_explicit
                        else stored_profile_selection(existing_profile_value)
                    )
                else:
                    fallback_profile = (
                        profile
                        if profile is not None
                        else existing.requested_profile or persisted_hermes.get("profile_name")
                    )
                fallback_model = (
                    model
                    or existing.requested_model
                    or (getattr(existing, "info", {}) or {}).get("model")
                    or persisted_hermes.get("model")
                    or persisted_info.get("model")
                )
                if not await self._validate_session_mapping(st_session_id, existing):
                    cwd = fallback_cwd
                    profile = fallback_profile
                    model = fallback_model
                else:
                    existing.mapping_validation_usage = None
                if restricted_profiles:
                    existing_effective_profile = stored_profile_selection(existing_profile_value)
                    profile_mismatch = existing_profile_disallowed or (
                        profile_was_explicit and existing_effective_profile != profile
                    )
                else:
                    profile_mismatch = bool(profile) and existing.requested_profile != profile
                cwd_mismatch = bool(cwd) and existing.requested_cwd != cwd
                context_mismatch = (
                    context_signature is not None
                    and existing.requested_context_signature != context_signature
                )
                if self.get_session(st_session_id) is not existing:
                    # The reconnect probe invalidated this mapping. Continue
                    # below and create only this session with the caller's
                    # visible history and runtime routing/context.
                    pass
                elif profile_mismatch or cwd_mismatch or context_mismatch:
                    if restricted_profiles and existing_profile_disallowed:
                        logger.info(
                            "Rebuilding Hermes session ST=%s because its profile is not allowed",
                            st_session_id,
                        )
                    else:
                        logger.info(
                            "Rebuilding Hermes session ST=%s for routing change "
                            "profile=%r->%r cwd=%r->%r context_changed=%s",
                            st_session_id,
                            existing.requested_profile,
                            profile,
                            existing.requested_cwd,
                            cwd,
                            context_mismatch,
                        )
                    await self.close_session(st_session_id)
                else:
                    await self._configure_tool_progress_mode(existing)
                    return tui_id
            else:
                self._invalidate_session(st_session_id, reason="missing local session object for mapped TUI session")

        if not self._should_connect or not self._ready:
            logger.error("Hermes is not connected or ready for ensure_session ST=%s", st_session_id)
            return None

        request_id = self._next_global_id()
        pending_key = f"__pending__{uuid.uuid4().hex[:8]}"
        session = HermesSession(tui_session_id=pending_key, st_session_id=st_session_id)
        effective_profile = profile if profile is not None else ("default" if restricted_profiles else None)
        # Hermes interprets an omitted profile as its launch profile, which can
        # be a named profile. Restricted mode therefore sends default explicitly.
        profile_override = session_profile_override(effective_profile)
        session.requested_cwd = cwd
        # Remember the policy's implicit default so a later reconnect can
        # distinguish a known-safe session from a legacy session with no label.
        session.requested_profile = effective_profile
        session.requested_model = model
        session.requested_context_signature = context_signature
        self._sessions[pending_key] = session
        self._pending_creations[pending_key] = st_session_id
        self._pending_requests[request_id] = st_session_id

        loop = asyncio.get_running_loop()
        creation_future = loop.create_future()
        self._creation_waiters[st_session_id] = creation_future

        params = {"cols": 80, "source": "sillytavern"}
        if cwd:
            params["cwd"] = cwd
        if profile_override:
            params["profile"] = profile_override
        if model:
            # Build a new session with its requested model from the outset.
            # Otherwise the immediate config.set below is recorded by Hermes
            # as a mid-chat switch even though no generated turn used the
            # profile default.
            params["model"] = model
        if messages is not None:
            params["messages"] = messages
        for key, value in (
            ("system_context", system_context),
            ("persona_context", persona_context),
            ("persona_reminder", persona_reminder),
            ("persona_version", persona_version),
        ):
            if value is not None:
                params[key] = value
        try:
            await self._send_json_rpc(request_id, "session.create", params)
        except Exception:
            self._cleanup_pending_creation(st_session_id)
            self._creation_waiters.pop(st_session_id, None)
            raise

        logger.info("Creating Hermes session ST=%s pending=%s", st_session_id, pending_key)

        # Wait for creation confirmation through the Future, resolved by _handle_response or _handle_session_info.
        try:
            tui_id = await asyncio.wait_for(creation_future, timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Timed out creating Hermes session for ST=%s", st_session_id)
            self._cleanup_pending_creation(st_session_id)
            return None
        finally:
            self._creation_waiters.pop(st_session_id, None)

        logger.info("Hermes session created ST=%s TUI=%s", st_session_id, tui_id)
        session = self._sessions.get(tui_id)
        if session:
            await self._configure_tool_progress_mode(session)
        return tui_id

    def _cleanup_pending_creation(self, st_session_id: str) -> None:
        """Remove all pending creation state for a given ST session."""
        pending_keys = [
            key for key, value in self._pending_creations.items() if value == st_session_id
        ]
        for pending_key in pending_keys:
            self._sessions.pop(pending_key, None)
            self._pending_creations.pop(pending_key, None)
            if self._st_to_tui.get(st_session_id) == pending_key:
                self._st_to_tui.pop(st_session_id, None)
        for req_id, session_id in list(self._pending_requests.items()):
            if session_id == st_session_id:
                self._pending_requests.pop(req_id, None)

    async def _configure_tool_progress_mode(self, session: HermesSession) -> None:
        """Request enough Hermes tool detail for the extension console."""
        mode = HERMES_TOOL_PROGRESS_MODE
        if not mode or session.tool_progress_mode_configured == mode:
            return
        if not self.is_connected or not self.is_ready:
            return
        try:
            result = await self._request_json_rpc(
                "config.set",
                {"session_id": session.tui_session_id, "key": "verbose", "value": mode},
                timeout=10.0,
            )
            configured = result.get("value") if isinstance(result, dict) else mode
            session.tool_progress_mode_configured = str(configured or mode)
            session.info = {
                **getattr(session, "info", {}),
                "tool_progress_mode": session.tool_progress_mode_configured,
            }
            self._persist_session_info(session)
        except (RuntimeError, asyncio.TimeoutError, ConnectionError) as exc:
            logger.warning(
                "Failed to configure Hermes tool progress mode ST=%s TUI=%s: %s",
                session.st_session_id,
                session.tui_session_id,
                exc,
            )

    async def _validate_session_mapping(self, st_session_id: str, session: HermesSession) -> bool:
        """Lazily validate one mapping after a new gateway connection.

        ``session.usage`` is read-only and session-scoped. A successful reply
        proves that Hermes kept the runtime session; a structured 4001 proves
        that only this mapping is stale. Transport failures remain ambiguous:
        they are propagated and never turn into a speculative rebuild.
        """
        if not session.mapping_needs_validation:
            return True

        validation_task = session.mapping_validation_task
        if validation_task is None or validation_task.done():
            validation_task = asyncio.create_task(self._probe_session_mapping(st_session_id, session))
            session.mapping_validation_task = validation_task
        return await asyncio.shield(validation_task)

    async def _probe_session_mapping(self, st_session_id: str, session: HermesSession) -> bool:
        try:
            result = await self._request_json_rpc(
                "session.usage",
                {"session_id": session.tui_session_id},
                timeout=5.0,
            )
        except RuntimeError as exc:
            if self._is_session_not_found_error(exc):
                self._invalidate_session(st_session_id, reason="session not found during reconnect validation")
                return False
            raise

        if self.get_session(st_session_id) is session:
            session.mapping_needs_validation = False
            session.mapping_validation_usage = result if isinstance(result, dict) else {}
        return True

    async def _ensure_mapping_for_operation(self, st_session_id: str) -> bool:
        session = self.get_session(st_session_id)
        if session is None:
            return False
        return await self._validate_session_mapping(st_session_id, session)

    async def _ensure_action_mapping_for_operation(self, st_session_id: str) -> bool:
        """Validate a live mapping and reject actions on revoked profile sessions."""
        if not await self._ensure_mapping_for_operation(st_session_id):
            return False
        session = self.get_session(st_session_id)
        if session is None:
            return False
        self._require_allowed_session_profile(session)
        return True

    async def close_session(self, st_session_id: str) -> None:
        """Close a Hermes session."""
        session = self.get_session(st_session_id)
        if session and not await self._validate_session_mapping(st_session_id, session):
            return
        await self._cancel_server_requests(st_session_id, reason="Session closed")
        tui_id = self._st_to_tui.get(st_session_id)
        if not tui_id:
            logger.debug("No Hermes session found for ST=%s", st_session_id)
            return

        session = self._sessions.get(tui_id)
        if session:
            # Signal in-flight generators to stop and clear their queues.
            # The session object is kept alive until the generator's `finally`
            # block runs, so we don't delete from _sessions here — we just
            # mark it as no longer processing and unmap it.
            for request_id in list(session.pending_queues):
                self._cleanup_prompt_request(session, request_id, queue_error="Session closed")
            if session.current_request_id is not None:
                self._cleanup_prompt_request(session, session.current_request_id)
            session.current_request_id = None
            session.is_processing = False
            session.turn_complete_event.set()
            self._sessions.pop(tui_id, None)

        self._st_to_tui.pop(st_session_id, None)

        if self._should_connect and self._ready and tui_id:
            await self._request_json_rpc("session.close", {"session_id": tui_id})
            logger.info("Hermes session closed ST=%s TUI=%s", st_session_id, tui_id)

    async def get_session_usage(self, st_session_id: str) -> dict[str, Any] | None:
        session = self.get_session(st_session_id)
        tui_id = session.tui_session_id if session else None
        if not tui_id or not self.is_connected or not self.is_ready:
            return None
        if session and session.mapping_needs_validation:
            if not await self._validate_session_mapping(st_session_id, session):
                return None
            usage = session.mapping_validation_usage
            session.mapping_validation_usage = None
            return usage if isinstance(usage, dict) else {}
        try:
            result = await self._request_json_rpc("session.usage", {"session_id": tui_id}, timeout=5.0)
        except RuntimeError as exc:
            if not self._is_session_not_found_error(exc):
                raise
            self._invalidate_session(st_session_id, reason="session not found during status probe")
            return None
        return result if isinstance(result, dict) else None

    async def get_session_info(self, st_session_id: str) -> dict[str, Any] | None:
        session = self.get_session(st_session_id)
        if not session:
            return None
        info = dict(getattr(session, "info", {}) or {})
        usage = await self.get_session_usage(st_session_id)
        if usage:
            info["usage"] = usage
        info["tui_session_id"] = session.tui_session_id
        return info

    async def model_options(self, st_session_id: str | None = None) -> dict[str, Any] | None:
        if not self.is_connected or not self.is_ready:
            return None
        tui_id = ""
        if st_session_id and await self._ensure_mapping_for_operation(st_session_id):
            tui_id = self._st_to_tui.get(st_session_id) or ""
        params = {"session_id": tui_id or ""}
        result = await self._request_json_rpc("model.options", params, timeout=10.0)
        return result if isinstance(result, dict) else None

    async def profile_options(self) -> dict[str, Any] | None:
        if not self.is_connected or not self.is_ready:
            return None
        result = await self._request_json_rpc("profiles.list", {}, timeout=10.0)
        return profile_options_for_client(result)

    async def set_profile(self, st_session_id: str, profile: str) -> dict[str, Any]:
        allowed = profile_allowlist()
        if allowed is not None:
            if not isinstance(profile, str):
                raise ProfileNotAllowedError()
            profile = profile.strip().lower()
            if not profile:
                raise ProfileNotAllowedError()
            resolve_profile_selection(profile)
        else:
            profile = str(profile or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile):
                raise ValueError("invalid profile name")
        if allowed is None or profile != "default":
            options = await self.profile_options()
            names = {
                str(item.get("name") or "")
                for item in (options or {}).get("profiles", [])
                if isinstance(item, dict)
            }
            if profile not in names:
                raise ValueError(f"unknown Hermes profile: {profile}")
        await self.close_session(st_session_id)
        info = SESSION_INFOS.setdefault(st_session_id, {})
        info.pop("hermes", None)
        info["profile"] = profile
        info["hermes_session_status"] = "rebuilding"
        info["updated_at"] = now_iso()
        _save_sessions()
        return {"profile": profile, "rebuild_required": True}

    async def set_model(
        self,
        st_session_id: str,
        model: str,
        *,
        cwd: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        model = str(model or "").strip()
        if not model:
            raise ValueError("model value required")
        tui_id = await self.ensure_session(st_session_id, cwd=cwd, profile=profile)
        if not tui_id:
            raise ConnectionError(f"Could not create Hermes session for ST={st_session_id}")
        session = self.get_session(st_session_id)
        current_model = str((getattr(session, "info", {}) or {}).get("model") or "").strip()
        if current_model == model:
            logger.debug("Hermes model already selected ST=%s model=%s", st_session_id, model)
            return {"key": "model", "value": model, "unchanged": True}
        result = await self._request_json_rpc(
            "config.set",
            {"session_id": tui_id, "key": "model", "value": model},
            timeout=30.0,
        )
        if isinstance(result, dict):
            if session:
                session.requested_model = model
                session.info = {**getattr(session, "info", {}), "model": result.get("value") or model}
                self._persist_session_info(session)
            return result
        return {"key": "model", "value": model}

    def get_session(self, st_session_id: str) -> Optional[HermesSession]:
        """Get a Hermes session by SillyTavern session ID."""
        tui_id = self._st_to_tui.get(st_session_id)
        return self._sessions.get(tui_id) if tui_id else None

    def has_prompt_history(self, st_session_id: str) -> bool:
        """Return whether the live Hermes session has received at least one prompt."""
        session = self.get_session(st_session_id)
        return bool(session and getattr(session, "submitted_prompt_count", 0) > 0)

    def session_status(self, st_session_id: str) -> str:
        if st_session_id in self._creation_waiters or st_session_id in self._pending_creations.values():
            return "rebuilding"

        session = self.get_session(st_session_id)
        if session:
            if session.is_processing:
                return "working"
            if not self.is_connected or not self.is_ready:
                return "unavailable"
            return "active"

        if not self.is_connected or not self.is_ready:
            return "unavailable"

        persisted_hermes = (SESSION_INFOS.get(st_session_id) or {}).get("hermes") or {}
        return "expired" if persisted_hermes.get("tui_session_id") else "not_started"

    def persist_session_state(self, st_session_id: str) -> None:
        session = self.get_session(st_session_id)
        if session:
            self._persist_session_info(session)

    async def interrupt_session(self, st_session_id: str) -> Any:
        if st_session_id in self._st_to_tui and not await self._ensure_action_mapping_for_operation(st_session_id):
            raise ValueError(f"No active Hermes session found for SillyTavern session {st_session_id}")
        tui_id = self._st_to_tui.get(st_session_id)
        if not tui_id:
            raise ValueError(f"No active Hermes session found for SillyTavern session {st_session_id}")
        session = self._sessions.get(tui_id)
        turn_complete_event = session.turn_complete_event if session else None
        result = await self._request_json_rpc(
            "session.interrupt",
            {"session_id": tui_id},
            timeout=15.0,
        )
        if turn_complete_event is not None and not turn_complete_event.is_set():
            try:
                await asyncio.wait_for(turn_complete_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Hermes terminal event not received after interrupt ST=%s TUI=%s",
                    st_session_id,
                    tui_id,
                )
        return result

    async def steer_session(self, st_session_id: str, text: str) -> Any:
        if st_session_id in self._st_to_tui and not await self._ensure_action_mapping_for_operation(st_session_id):
            raise ValueError(f"No active Hermes session found for SillyTavern session {st_session_id}")
        tui_id = self._st_to_tui.get(st_session_id)
        if not tui_id:
            raise ValueError(f"No active Hermes session found for SillyTavern session {st_session_id}")
        text = str(text or "").strip()
        if not text:
            raise ValueError("steer text required")
        return await self._request_json_rpc(
            "session.steer",
            {"session_id": tui_id, "text": text},
            timeout=15.0,
        )

    async def undo_session(self, st_session_id: str) -> Any:
        if st_session_id in self._st_to_tui and not await self._ensure_action_mapping_for_operation(st_session_id):
            return {"removed": 0, "session_expired": True}
        tui_id = self._st_to_tui.get(st_session_id)
        if not tui_id:
            return {"removed": 0, "session_expired": True}
        try:
            result = await self._request_json_rpc(
                "session.undo",
                {"session_id": tui_id},
                timeout=15.0,
            )
        except RuntimeError as exc:
            if not self._is_session_not_found_error(exc):
                raise
            self._invalidate_session(st_session_id, reason="session not found during undo")
            return {"removed": 0, "session_expired": True}
        session = self.get_session(st_session_id)
        if session and session.submitted_prompt_count > 0:
            session.submitted_prompt_count -= 1
        return result

    async def compress_session(self, st_session_id: str, focus_topic: str = "") -> Any:
        if st_session_id in self._st_to_tui and not await self._ensure_action_mapping_for_operation(st_session_id):
            return {"skipped": True, "session_expired": True, "reason": "No live Hermes context to compress"}
        tui_id = self._st_to_tui.get(st_session_id)
        if not tui_id:
            return {"skipped": True, "session_expired": True, "reason": "No live Hermes context to compress"}
        try:
            return await self._request_json_rpc(
                "session.compress",
                {"session_id": tui_id, "focus_topic": str(focus_topic or "").strip()},
                timeout=120.0,
            )
        except RuntimeError as exc:
            if not self._is_session_not_found_error(exc):
                raise
            self._invalidate_session(st_session_id, reason="session not found during compression")
            return {"skipped": True, "session_expired": True, "reason": "No live Hermes context to compress"}

    @staticmethod
    def _normalized_rpc_code(code: Any) -> str:
        if isinstance(code, bool) or code is None:
            return ""
        return str(code).strip()

    @staticmethod
    def _normalized_rpc_message(message: Any) -> str:
        if not isinstance(message, str):
            return ""
        return " ".join(message.split()).casefold()

    @classmethod
    def _is_session_not_found_error(cls, exc: BaseException) -> bool:
        """Classify only the explicit, unambiguous Hermes stale-session error."""
        if isinstance(exc, HermesJsonRpcError):
            return (
                cls._normalized_rpc_code(exc.code) == "4001"
                and cls._normalized_rpc_message(exc.rpc_message) == "session not found"
            )

        raw_text = str(exc)
        try:
            parsed = json.loads(raw_text)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            envelope = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
            if not isinstance(envelope, dict):
                return False
            return (
                cls._normalized_rpc_code(envelope.get("code")) == "4001"
                and cls._normalized_rpc_message(envelope.get("message")) == "session not found"
            )

        return cls._normalized_rpc_message(raw_text) == "hermes error 4001: session not found"

    def _invalidate_session(self, st_session_id: str, *, reason: str) -> None:
        tui_id = self._st_to_tui.pop(st_session_id, None)
        for rpc_id, record in list(self._server_requests.items()):
            if record.st_session_id == st_session_id and (tui_id is None or record.tui_session_id == tui_id):
                self._server_requests.pop(rpc_id, None)
                self._server_request_locks.pop(rpc_id, None)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(
                        ws_broadcast(
                            record.st_session_id,
                            {
                                "type": "server_request_cancel",
                                "session_id": record.st_session_id,
                                "rpc_id": record.rpc_id,
                                "method": record.method,
                                "reason": "Hermes request cancelled",
                            },
                        )
                    )
        session = self._sessions.pop(tui_id, None) if tui_id else None
        if session:
            for request_id in list(session.pending_queues):
                self._cleanup_prompt_request(session, request_id, queue_error="Session mapping invalidated")
            session.current_request_id = None
            session.is_processing = False
            session.turn_complete_event.set()
            session.mapping_needs_validation = False
            session.mapping_validation_usage = None
        logger.info(
            "Invalidated stale Hermes session ST=%s TUI=%s reason=%s",
            st_session_id,
            tui_id,
            reason,
        )

    def _find_session_by_tui(self, tui_id: str) -> Optional[HermesSession]:
        """Find a session by its tui_session_id."""
        if not tui_id:
            return None
        # Direct lookup
        session = self._sessions.get(tui_id)
        if session:
            return session
        # Lookup by tui_session_id
        for session in self._sessions.values():
            if session.tui_session_id == tui_id:
                return session
        return None

    # ─── Prompt submission ───────────────────────────────────

    async def _submit_prompt_once(
        self,
        st_session_id: str,
        text: str,
        image_attachments: list[dict[str, Any]] | None = None,
        *,
        system_context: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        persona_context: str | None = None,
        persona_reminder: str | None = None,
        persona_version: str | None = None,
        workspace_cwd: str | None = None,
        profile: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Submit a prompt and stream events through an async generator.

        First ensure that the session exists, creating it if needed.

        Yields:
            Event dictionaries:
            - {"type": "text", "text": "..."}
            - {"type": "reasoning", "text": "..."}
            - {"type": "thinking", "text": "..."}
            - {"type": "tool.start", "tool": {...}}
            - {"type": "tool.complete", "tool": {...}}
            - {"type": "accepted", "status": "queued|steered|redirected", "retry": False}
            - {"type": "done", "status": "complete|error|interrupted"}
        """
        # Ensure the session exists
        tui_id = await self.ensure_session(
            st_session_id,
            cwd=workspace_cwd,
            profile=profile,
            model=model,
            messages=conversation_history,
            system_context=system_context,
            persona_context=persona_context,
            persona_reminder=persona_reminder,
            persona_version=persona_version,
        )
        if not tui_id:
            yield {"type": "error", "message": f"Could not create Hermes session for ST={st_session_id}"}
            return

        session = self.get_session(st_session_id)
        if not session:
            yield {"type": "error", "message": f"No Hermes session found for ST={st_session_id}"}
            return

        if not self._should_connect or not self._ready:
            yield {"type": "error", "message": "Hermes is not connected"}
            return

        if session.is_processing:
            yield {"type": "error", "message": f"Session {st_session_id} is already processing a request"}
            return

        images = image_attachments or []
        for index, image in enumerate(images, start=1):
            content_base64 = image.get("content_base64")
            if not content_base64:
                yield {"type": "error", "message": f"Image attachment {index} has no content"}
                return
            params = {
                "session_id": session.tui_session_id,
                "content_base64": content_base64,
                "filename": image.get("filename") or f"sillytavern_image_{index}.png",
            }
            try:
                result = await self._request_json_rpc("image.attach_bytes", params, timeout=30.0)
            except RuntimeError as exc:
                if self._is_session_not_found_error(exc):
                    raise HermesSessionNotFoundError(exc) from exc
                logger.warning(
                    "Hermes image attach failed ST=%s TUI=%s index=%d error_type=%s",
                    st_session_id,
                    session.tui_session_id,
                    index,
                    type(exc).__name__,
                )
                yield {"type": "error", "message": f"Image attachment failed: {_safe_rpc_exception_text(exc)}"}
                return
            except Exception as exc:
                logger.warning(
                    "Hermes image attach failed ST=%s TUI=%s index=%d error_type=%s",
                    st_session_id,
                    session.tui_session_id,
                    index,
                    type(exc).__name__,
                )
                yield {"type": "error", "message": f"Image attachment failed: {exc}"}
                return
            if not isinstance(result, dict) or not result.get("attached"):
                message = result.get("message") if isinstance(result, dict) else "unknown error"
                yield {"type": "error", "message": f"Image attachment failed: {message}"}
                return

        if images:
            logger.info(
                "Hermes image attachments queued ST=%s TUI=%s count=%d",
                st_session_id,
                session.tui_session_id,
                len(images),
            )

        # Create the queue for this request
        request_id = self._next_global_id()
        session.turn_complete_event.clear()
        session.current_request_id = request_id
        session.is_processing = True

        event_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        session.pending_queues[request_id] = event_queue

        # Idle timeout for the prompt without any Hermes event.
        _last_event_time = asyncio.get_event_loop().time()
        _idle_timeout = float(BACKEND_CONFIGS["hermes"].get("stream_idle_timeout") or 120.0)
        ack_waiter = asyncio.get_running_loop().create_future()
        self._rpc_waiters[request_id] = ack_waiter

        try:
            # Phase 2: send the request and wait only for its JSON-RPC ACK.  No
            # streaming callback/event processing belongs in this exception
            # scope: after this point, RuntimeError must keep its real meaning.
            params = {"session_id": session.tui_session_id, "text": text}
            try:
                await self._send_json_rpc(request_id, "prompt.submit", params)
                ack = await asyncio.wait_for(
                    ack_waiter,
                    timeout=PROMPT_SUBMIT_ACK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                yield {
                    "type": "error",
                    "message": (
                        "[Proxy] Prompt submission acknowledgement timed out; "
                        "submission state is uncertain and was not retried."
                    ),
                }
                return
            except RuntimeError as exc:
                if self._is_session_not_found_error(exc):
                    raise HermesSessionNotFoundError(exc) from exc
                yield {"type": "error", "message": f"[Hermes] Prompt submission rejected: {_safe_rpc_exception_text(exc)}"}
                return
            except ConnectionError:
                yield {
                    "type": "error",
                    "message": (
                        "[Proxy] Hermes connection lost while waiting for prompt submission acknowledgement; "
                        "submission state is uncertain and was not retried."
                    ),
                }
                return
            except (OSError, websockets.exceptions.WebSocketException):
                yield {
                    "type": "error",
                    "message": (
                        "[Proxy] Hermes connection lost while waiting for prompt submission acknowledgement; "
                        "submission state is uncertain and was not retried."
                    ),
                }
                return

            # Phase 3: interpret the concrete PromptSubmitResult contract.
            if not isinstance(ack, dict):
                yield {
                    "type": "error",
                    "message": "[Hermes] Prompt submission protocol error: acknowledgement is not an object.",
                }
                return
            if ack.get("voice_stopped") is True:
                # This is the typed stop-phrase response; Hermes did not
                # submit a prompt and therefore there is no stream to await.
                yield {"type": "done", "status": "complete", "payload": ack}
                return

            status = ack.get("status")
            if not isinstance(status, str) or status not in _PROMPT_SUBMIT_ACCEPTED_STATUSES:
                yield {
                    "type": "error",
                    "message": "[Hermes] Prompt submission protocol error: unknown acknowledgement result.",
                }
                return

            # Hermes counts all four statuses as accepted submissions: the
            # non-streaming states represent a prompt that was queued, steered,
            # or redirected, not a rejected request.
            session.submitted_prompt_count += 1
            logger.info(
                "Prompt submitted ST=%s TUI=%s req=%d text_len=%d system_len=%d history_messages=%d persona_len=%d persona_reminder_len=%d prompt_count=%d",
                st_session_id,
                session.tui_session_id,
                request_id,
                len(text),
                len(system_context or ""),
                len(conversation_history or []),
                len(persona_context or ""),
                len(persona_reminder or ""),
                session.submitted_prompt_count,
            )

            if status != _PROMPT_SUBMIT_STREAM_STATUS:
                # The bridge serializes submissions with session.is_processing,
                # so queued/steered/redirected are normally unexpected here.
                # They remain valid Hermes outcomes for races or future gateway
                # policies.  No dedicated stream can be guaranteed, so finish
                # explicitly and never make the frontend retry the prompt.
                yield {
                    "type": "accepted",
                    "accepted": True,
                    "retry": False,
                    "status": status,
                    "message": _PROMPT_SUBMIT_NON_STREAM_MESSAGES[status],
                }
                return

            # Phase 4: stream events only after a streaming ACK.
            while True:
                # Check idle timeout before each loop
                if asyncio.get_event_loop().time() - _last_event_time > _idle_timeout:
                    logger.warning(
                        "submit_prompt idle timeout ST=%s TUI=%s after %.1fs",
                        st_session_id, session.tui_session_id, _idle_timeout,
                    )
                    yield {"type": "error", "message": f"[Proxy] Prompt timed out after {_idle_timeout:.0f}s of inactivity"}
                    break

                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=10.0)
                    _last_event_time = asyncio.get_event_loop().time()
                except asyncio.TimeoutError:
                    # No event received; check whether processing has finished.
                    if not session.is_processing:
                        break
                    continue

                if event["type"] == "message.complete":
                    status = event.get("payload", {}).get("status", "complete")
                    yield {"type": "done", "status": status, "payload": event.get("payload", {})}
                    break

                elif event["type"] == "error":
                    yield {"type": "error", "message": event.get("message", "Unknown error")}
                    break

                elif event["type"] == "message.delta":
                    yield {"type": "text", "text": event["text"]}

                elif event["type"] == "thinking.delta":
                    yield {"type": "thinking_status", "text": event["text"]}

                elif event["type"] == "reasoning.delta":
                    cleaned_text = _clean_reasoning_text(event["text"])
                    if cleaned_text:
                        yield {"type": "reasoning", "text": cleaned_text}
                    else:
                        yield {"type": "thinking_status", "text": event["text"]}

                elif event["type"] == "status.update":
                    yield {"type": "status", "payload": event.get("payload", {})}

                elif event["type"] == "tool.start":
                    yield {"type": "tool.start", "tool": event["tool"]}

                elif event["type"] == "tool.complete":
                    yield {"type": "tool.complete", "tool": event["tool"]}

        finally:
            self._cleanup_prompt_request(session, request_id)

    async def submit_prompt(
        self,
        st_session_id: str,
        text: str,
        image_attachments: list[dict[str, Any]] | None = None,
        *,
        system_context: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        persona_context: str | None = None,
        persona_reminder: str | None = None,
        persona_version: str | None = None,
        workspace_cwd: str | None = None,
        profile: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Submit a prompt, allowing one safe recovery from a stale mapping."""
        images = image_attachments or []
        for attempt in range(2):
            profile = resolve_profile_selection(profile)
            try:
                async for event in self._submit_prompt_once(
                    st_session_id,
                    text,
                    images,
                    system_context=system_context,
                    conversation_history=conversation_history,
                    persona_context=persona_context,
                    persona_reminder=persona_reminder,
                    persona_version=persona_version,
                    workspace_cwd=workspace_cwd,
                    profile=profile,
                    model=model,
                ):
                    yield event
                return
            except HermesSessionNotFoundError as exc:
                if attempt:
                    self._invalidate_session(st_session_id, reason="session not found after prompt recovery")
                    yield {"type": "error", "message": f"[Hermes] Prompt submission rejected: {exc.safe_message}"}
                    return

                session = self.get_session(st_session_id)
                retry_cwd = workspace_cwd
                retry_profile = profile
                retry_model = model
                if session:
                    retry_cwd = retry_cwd if retry_cwd is not None else session.requested_cwd
                    retry_profile = retry_profile if retry_profile is not None else stored_profile_selection(
                        session.requested_profile
                    )
                    retry_model = (
                        retry_model
                        or session.requested_model
                        or (getattr(session, "info", {}) or {}).get("model")
                    )
                persisted_info = SESSION_INFOS.get(st_session_id) or {}
                self._clear_disallowed_persisted_profile(persisted_info)
                persisted_hermes_value = persisted_info.get("hermes")
                persisted_hermes: dict[str, Any] = persisted_hermes_value if isinstance(persisted_hermes_value, dict) else {}
                retry_cwd = retry_cwd if retry_cwd is not None else persisted_hermes.get("cwd")
                retry_profile = retry_profile if retry_profile is not None else stored_profile_selection(
                    persisted_hermes.get("profile_name")
                )
                retry_model = retry_model or persisted_hermes.get("model") or persisted_info.get("model")
                self._invalidate_session(st_session_id, reason="session not found during prompt recovery")
                workspace_cwd, profile, model = retry_cwd, retry_profile, retry_model

    # ─── JSON-RPC transport ──────────────────────────────────

    async def _send_json_rpc(self, req_id: int, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC 2.0 request over the WebSocket connection."""
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        if not self._ws or not self.is_connected:
            raise ConnectionError("Hermes WebSocket connection closed")

        await self._ws.send(json.dumps(msg, ensure_ascii=False))
        logger.debug("JSON-RPC sent: method=%s id=%s", method, req_id)

    async def _request_json_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        req_id = self._next_global_id()
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._rpc_waiters[req_id] = waiter
        try:
            await self._send_json_rpc(req_id, method, params)
            return await asyncio.wait_for(waiter, timeout=timeout)
        finally:
            self._rpc_waiters.pop(req_id, None)

    def _next_global_id(self) -> int:
        """Generate a globally unique request ID."""
        self._global_id += 1
        return self._global_id

    # ─── Callbacks ───────────────────────────────────────────

    def on_tool_call(self, callback: Callable) -> None:
        """Register a callback for tool-call events."""
        self._on_tool_call.append(callback)

    def on_reasoning(self, callback: Callable) -> None:
        """Register a callback for reasoning events."""
        self._on_reasoning.append(callback)

    def on_text(self, callback: Callable) -> None:
        """Register a callback for text events."""
        self._on_text.append(callback)

    # ─── Status ──────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        if not self._should_connect or self._ws is None:
            return False
        # websockets <14 uses .open (bool)
        if hasattr(self._ws, "open"):
            return self._ws.open
        # websockets >=14 uses .state (State enum)
        if hasattr(self._ws, "state"):
            state = self._ws.state
            return getattr(state, "name", "") == "OPEN" or getattr(state, "value", 0) == 1
        return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def session_count(self) -> int:
        return len([s for s in self._sessions.values() if not s.tui_session_id.startswith("__pending__")])
