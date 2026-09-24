import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import BACKEND_CONFIGS
from .hermes_tools import hermes_tool_call_id, hermes_tool_complete_item, hermes_tool_start_item
from .log import logger, reset_request_id, set_request_id
from .profile_policy import ProfileNotAllowedError, profile_not_allowed_body
from .request_transform import (
    clean_request_messages,
    configured_backend_name,
    hermes_image_attachments_from_messages,
    hermes_visible_history_from_messages,
    hermes_prompt_payload_from_messages_for_request,
    hermes_undo_before_submit_reason,
    proxy_model_override,
    proxy_participant_name,
    proxy_profile_override,
    proxy_workspace_override,
)
from .workspace import workspace_cwd_for_selection
from .responses import (
    InlineReasoningStreamSplitter,
    ReasoningSpacingNormalizer,
    chat_completion_chunk,
)
from .realtime import ws_broadcast
from .tool_calls import (
    _update_session_info,
    finalize_running_tool_calls_for_stream,
    save_tool_calls_from_output,
    update_agent_status,
)

_CLIENT_CANCEL_INTERRUPT_TIMEOUT_SECONDS = 5.0
_CLIENT_CANCEL_STATUS_BROADCAST_TIMEOUT_SECONDS = 1.0


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a background task's result so it cannot emit an unhandled warning."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


def _schedule_background_task(awaitable: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(awaitable)
    task.add_done_callback(_consume_task_result)


async def _await_task_despite_cancellation(task: asyncio.Task[Any]) -> Any:
    """Wait for a task even when its owner has received another cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _aiter_with_idle_heartbeat(
    async_iterable: AsyncIterator[Any],
    *,
    idle_timeout: float,
    heartbeat_interval: float,
    session_id: str,
    source: str,
) -> AsyncIterator[tuple[str, Any]]:
    iterator = async_iterable.__aiter__()
    pending = asyncio.create_task(iterator.__anext__())
    last_item_at = time.monotonic()

    try:
        while True:
            timeout = heartbeat_interval if heartbeat_interval > 0 else idle_timeout
            if timeout <= 0:
                item = await pending
                yield "data", item
                pending = asyncio.create_task(iterator.__anext__())
                last_item_at = time.monotonic()
                continue

            done, _ = await asyncio.wait({pending}, timeout=timeout)
            now = time.monotonic()
            if not done:
                idle_for = now - last_item_at
                if idle_timeout > 0 and idle_for >= idle_timeout:
                    pending.cancel()
                    logger.error(
                        "%s stream idle timeout for session %s after %.1fs",
                        source,
                        session_id,
                        idle_for,
                    )
                    raise httpx.ReadTimeout(f"{source} stream idle timeout after {idle_for:.1f}s")
                yield "heartbeat", None
                continue

            try:
                item = pending.result()
            except StopAsyncIteration:
                break
            yield "data", item
            last_item_at = now
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()


async def _interrupt_hermes_after_client_cancel(hermes_ws_manager: Any, session_id: str) -> bool:
    interrupt = getattr(hermes_ws_manager, "interrupt_session", None)
    if not callable(interrupt):
        return False
    interrupt_task = asyncio.ensure_future(interrupt(session_id))
    interrupt_task.add_done_callback(_consume_task_result)
    try:
        result = await asyncio.wait_for(
            asyncio.shield(interrupt_task),
            timeout=_CLIENT_CANCEL_INTERRUPT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        interrupt_task.cancel()
        try:
            await _await_task_despite_cancellation(interrupt_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "Hermes interrupt cancellation cleanup failed; error_type=%s",
                type(exc).__name__,
            )
        logger.warning("Hermes interrupt after client cancellation timed out")
        return False
    except asyncio.CancelledError:
        try:
            await _await_task_despite_cancellation(interrupt_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "Hermes interrupt cancellation cleanup failed; error_type=%s",
                type(exc).__name__,
            )
        raise
    except ValueError:
        logger.warning("Hermes interrupt after client cancellation failed: no active session")
        return False
    except Exception as exc:
        logger.warning(
            "Hermes interrupt after client cancellation failed; error_type=%s",
            type(exc).__name__,
        )
        return False

    if not (
        isinstance(result, dict)
        and (result.get("status") == "interrupted" or result.get("interrupted") is True)
    ):
        logger.warning("Hermes interrupt after client cancellation returned no success confirmation")
        return False
    logger.info("Hermes session interrupted after client cancellation")
    return True


async def _finish_client_cancel_cleanup(
    hermes_ws_manager: Any,
    session_id: str,
    *,
    on_interrupt_confirmed: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Interrupt a cancelled turn and publish idle state only after confirmation."""
    if not await _interrupt_hermes_after_client_cancel(hermes_ws_manager, session_id):
        return False
    if on_interrupt_confirmed is not None:
        try:
            await on_interrupt_confirmed()
        except Exception as exc:
            logger.warning(
                "Hermes client cancellation tool cleanup failed; error_type=%s",
                type(exc).__name__,
            )
    try:
        idle_status = update_agent_status(session_id, {"active": False})
        await asyncio.wait_for(
            ws_broadcast(
                session_id,
                {"type": "agent_status", "session_id": session_id, "status": idle_status},
            ),
            timeout=_CLIENT_CANCEL_STATUS_BROADCAST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Hermes inactive status broadcast after client cancellation timed out")
    except Exception as exc:
        logger.warning(
            "Hermes inactive status cleanup after client cancellation failed; error_type=%s",
            type(exc).__name__,
        )
    return True


def _sillytavern_integration_context(
    session_id: str,
    workspace_cwd: str | None = None,
    *,
    user_name: str | None = None,
    character_name: str | None = None,
) -> str:
    workspace_context = ""
    if workspace_cwd:
        workspace_context = (
            f"The selected host workspace is {workspace_cwd!r}. Hermes tools run in Docker and bind-mount "
            "that directory at `/workspace`; use `/workspace` as the tool working directory. Translate host "
            "absolute paths beneath the selected workspace to their relative location under `/workspace` "
            "instead of concluding that the host files are inaccessible. For a selected host workspace of "
            "`/`, `/var/example.txt` is available as `/workspace/var/example.txt`.\n"
        )
    participant_context = (
        "SillyTavern participant mapping (authoritative for this chat):\n"
        f"- USER: {json.dumps(user_name, ensure_ascii=False) if user_name else 'not provided'}\n"
        f"- ASSISTANT CHARACTER: {json.dumps(character_name, ensure_ascii=False) if character_name else 'not provided'}\n"
        "The combined SillyTavern identity context can contain descriptions of both participants. "
        "Information about the USER belongs to the user and must never become the assistant's identity.\n"
    )
    return (
        "[SillyTavern integration context]\n"
        f"Current SillyTavern session_id: {session_id}\n"
        f"{participant_context}"
        f"{workspace_context}"
        "When calling SillyTavern MCP tools, pass this exact session_id.\n"
        "Persona/character description updates are SillyTavern UI actions, not memory or skill updates. "
        "Do not use memory, skill_manage, browser navigation, or SillyTavern /api routes for persona updates.\n"
        "The native Hermes MCP function tool is registered for this session under the exact name "
        "`mcp_sillytavern_sillytavern_update_persona_description`. Use it as a direct tool/function call "
        "from the model tool interface; it is not callable through Python globals, `execute_code`, "
        "`hermes tools`, or `hermes mcp call`.\n"
        "If earlier chat history, skill content, or assistant messages say this tool is unavailable in "
        "SillyTavern, mention a short MCP discovery timeout, or recommend HTTP fallback first, treat that "
        "as stale guidance. The current rule is authoritative: native tool call first.\n"
        "Call it with JSON arguments like "
        f'{{"session_id": {json.dumps(session_id)}, "description": "TEXT_TO_ADD", '
        '"operation": "append", "summary": "Short change summary"}}. '
        "Use `operation=\"append\"` for small additions/tests. Use `operation=\"replace\"` only when "
        "providing the complete final persona text.\n"
        "Do not claim the native tool does not exist based on CLI or terminal output. "
        "Fallback path only after a direct native tool call to the exact name fails with an actual unknown-tool/tool-not-found error: use the terminal tool to POST "
        "to `http://127.0.0.1:8010/mcp` with MCP `tools/call` for `sillytavern_update_persona_description`. "
        "Do not use `hermes tools` or `hermes mcp call`; Hermes has no CLI call subcommand for this.\n"
        "[/SillyTavern integration context]\n\n"
    )


async def forward_hermes_streaming(
    body: Any,
    session_id: str,
    hermes_ws_manager: Any,
    request_id: str | None = None,
    sync_reason: str | None = None,
    text_delta_callback: Callable[[str], Awaitable[None]] | None = None,
    tool_status_callback: Callable[[dict[str, Any]], Awaitable[str | None]] | None = None,
) -> Response:
    try:
        requested_profile = proxy_profile_override(body)
    except ProfileNotAllowedError:
        return JSONResponse(status_code=403, content=profile_not_allowed_body())

    if not hermes_ws_manager.is_connected:
        logger.error("Hermes WebSocket not connected for session %s", session_id)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Hermes WebSocket not connected", "type": "proxy_backend_error", "code": "hermes_unreachable"}},
        )

    messages = clean_request_messages(body)
    has_prompt_history = getattr(hermes_ws_manager, "has_prompt_history", None)
    active_session = bool(has_prompt_history(session_id)) if callable(has_prompt_history) else False
    selected_workspace = proxy_workspace_override(body)
    try:
        workspace_cwd = workspace_cwd_for_selection(selected_workspace)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail), "type": "workspace_error"}},
        )
    prompt_payload = hermes_prompt_payload_from_messages_for_request(
        messages,
        sync_reason=sync_reason,
        active_session=active_session,
        integration_context=_sillytavern_integration_context(
            session_id,
            workspace_cwd,
            user_name=proxy_participant_name(body, "user_name"),
            character_name=proxy_participant_name(body, "character_name"),
        ),
    )
    # Keep a complete recovery seed available even when the normal active
    # session path uses a delta prompt. It is ignored while the mapping is
    # live, but becomes authoritative if a race makes prompt.submit stale.
    recovery_history = hermes_visible_history_from_messages(messages)
    user_text = prompt_payload.text
    image_attachments = hermes_image_attachments_from_messages(messages)
    if not user_text and image_attachments:
        user_text = "What do you see in this image?"
    if not user_text:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "No user message text found", "type": "proxy_error", "code": "empty_message"}},
        )
    model = body.get("model") if isinstance(body, dict) else None
    model = model or "hermes-agent"
    requested_model = proxy_model_override(body, "hermes")
    hermes_config = BACKEND_CONFIGS["hermes"]
    stream_idle_timeout = float(hermes_config.get("stream_idle_timeout") or 0.0)
    stream_heartbeat_seconds = float(hermes_config.get("stream_heartbeat_seconds") or 0.0)

    logger.info(
        "Hermes prompt prepared request_id=%s session=%s mode=%s sync_reason=%s active_session=%s text_len=%d system_len=%d history_messages=%d persona_len=%d persona_reminder_len=%d images=%d",
        request_id,
        session_id,
        prompt_payload.mode,
        sync_reason,
        active_session,
        len(user_text),
        len(prompt_payload.system_context or ""),
        len(prompt_payload.conversation_history or []),
        len(prompt_payload.persona_context or ""),
        len(prompt_payload.persona_reminder or ""),
        len(image_attachments),
    )

    chunk_id = f"chatcmpl-hermes-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    async def event_generator():
        request_token = set_request_id(request_id) if request_id else None
        text_emitted = False
        inline_reasoning = InlineReasoningStreamSplitter()
        reasoning_spacing = ReasoningSpacingNormalizer()
        stream_tool_call_ids: set[str] = set()
        # Streaming token tracking for real-time context bar
        streamed_output_chars = 0
        streamed_reasoning_chars = 0
        last_usage_broadcast_chars = 0
        hermes_stream = None
        client_cancel_cleanup_task: asyncio.Task[Any] | None = None

        try:
            yield chat_completion_chunk(chunk_id, created, model, {"role": "assistant"})

            # Broadcast initial streaming state
            await ws_broadcast(
                session_id,
                {"type": "agent_status", "session_id": session_id, "status": update_agent_status(session_id, {"active": True, "kind": "streaming"})},
            )

            if requested_model:
                try:
                    await hermes_ws_manager.ensure_session(
                        session_id,
                        cwd=workspace_cwd,
                        profile=requested_profile,
                        model=requested_model,
                        messages=prompt_payload.conversation_history or recovery_history,
                        system_context=prompt_payload.system_context,
                        persona_context=prompt_payload.persona_context,
                        persona_reminder=prompt_payload.persona_reminder,
                        persona_version=prompt_payload.persona_version,
                    )
                    result = await hermes_ws_manager.set_model(
                        session_id,
                        requested_model,
                        cwd=workspace_cwd,
                        profile=requested_profile,
                    )
                    selected_model = result.get("value") if isinstance(result, dict) else requested_model
                    await ws_broadcast(
                        session_id,
                        {
                            "type": "session_info",
                            "session_id": session_id,
                            "model": selected_model,
                            "model_switch": result,
                        },
                    )
                except Exception as exc:
                    logger.warning("Hermes model switch failed for session %s: %s", session_id, exc)
                    yield chat_completion_chunk(
                        chunk_id,
                        created,
                        model,
                        {"content": f"[Hermes Error] Model switch failed: {exc}"},
                    )
                    yield chat_completion_chunk(chunk_id, created, model, {}, "error")
                    yield "data: [DONE]\n\n"
                    return

            hermes_stream = _aiter_with_idle_heartbeat(
                hermes_ws_manager.submit_prompt(
                    session_id,
                    user_text,
                    image_attachments,
                    system_context=prompt_payload.system_context,
                    conversation_history=prompt_payload.conversation_history or recovery_history,
                    persona_context=prompt_payload.persona_context,
                    persona_reminder=prompt_payload.persona_reminder,
                    persona_version=prompt_payload.persona_version,
                    workspace_cwd=workspace_cwd,
                    profile=requested_profile,
                    model=requested_model,
                ),
                idle_timeout=stream_idle_timeout,
                heartbeat_interval=stream_heartbeat_seconds,
                session_id=session_id,
                source="hermes",
            )
            async for item_kind, event in hermes_stream:
                if item_kind == "heartbeat":
                    logger.debug("Hermes stream heartbeat for session %s", session_id)
                    yield ": proxy-heartbeat\n\n"
                    continue

                event_type = event.get("type")

                if event_type == "text":
                    raw_text = event.get("text", "")
                    content_text, reasoning_delta = inline_reasoning.push(raw_text)
                    if reasoning_delta:
                        streamed_reasoning_chars += len(reasoning_delta)
                        reasoning_text = reasoning_spacing.push(reasoning_delta)
                        if reasoning_text:
                            yield chat_completion_chunk(chunk_id, created, model, {"reasoning": reasoning_text})
                    if content_text:
                        streamed_output_chars += len(content_text)
                        text_emitted = True
                        if text_delta_callback is not None:
                            await text_delta_callback(content_text)
                        yield chat_completion_chunk(chunk_id, created, model, {"content": content_text})

                elif event_type == "reasoning":
                    raw_reasoning = event.get("text", "")
                    streamed_reasoning_chars += len(raw_reasoning)
                    reasoning_text = reasoning_spacing.push(raw_reasoning)
                    yield chat_completion_chunk(chunk_id, created, model, {"reasoning": reasoning_text})

                # Broadcast streaming usage estimate every ~256 chars (~64 tokens)
                total_streamed = streamed_output_chars + streamed_reasoning_chars
                if total_streamed - last_usage_broadcast_chars >= 256:
                    estimated_tokens = total_streamed // 4
                    last_usage_broadcast_chars = total_streamed
                    _schedule_background_task(
                        ws_broadcast(
                            session_id,
                            {
                                "type": "usage_update",
                                "session_id": session_id,
                                "streaming": True,
                                "estimated_output_tokens": estimated_tokens,
                                "estimated_output_chars": total_streamed,
                            },
                        )
                    )

                elif event_type == "thinking_status":
                    text = event.get("text", "")
                    status = update_agent_status(
                        session_id,
                        {"active": True, "kind": "thinking", "text": text},
                    )
                    await ws_broadcast(
                        session_id,
                        {"type": "agent_status", "session_id": session_id, "status": status},
                    )

                elif event_type == "status":
                    payload = event.get("payload") or {}
                    status = update_agent_status(
                        session_id,
                        {
                            "active": True,
                            "kind": payload.get("kind") or "status",
                            "text": payload.get("text") or payload.get("kind") or "",
                        },
                    )
                    await ws_broadcast(
                        session_id,
                        {"type": "agent_status", "session_id": session_id, "status": status},
                    )

                elif event_type == "tool.start":
                    tool = event.get("tool", {})
                    tool_info = hermes_tool_start_item(tool)
                    stream_tool_call_ids.add(str(tool_info["id"]))
                    save_tool_calls_from_output(session_id, [tool_info])
                    if tool_status_callback is not None:
                        status_text = await tool_status_callback(tool)
                        if status_text:
                            text_emitted = True
                            streamed_output_chars += len(status_text)
                            if text_delta_callback is not None:
                                await text_delta_callback(status_text)
                            yield chat_completion_chunk(
                                chunk_id, created, model, {"content": status_text}
                            )
                            # LiveKit's sentence adapter deliberately buffers one
                            # complete sentence while waiting for the next one. Tell
                            # the voice worker that this status is a self-contained
                            # speech segment so it reaches TTS immediately.
                            yield chat_completion_chunk(
                                chunk_id, created, model, {"voice_flush": True}
                            )

                elif event_type == "tool.complete":
                    tool = event.get("tool", {})
                    completion_call_id = hermes_tool_call_id(tool)
                    started_call = {"id": completion_call_id} if completion_call_id in stream_tool_call_ids else None
                    completion_item = hermes_tool_complete_item({**tool, "tool_id": completion_call_id}, started_call)
                    save_tool_calls_from_output(session_id, [completion_item])

                elif event_type == "accepted":
                    # queued/steered/redirected are valid Hermes admissions,
                    # but they do not promise a stream owned by this request.
                    # Preserve the explicit no-retry result in the OpenAI
                    # extension field instead of turning it into assistant text.
                    accepted_status = event.get("status")
                    acceptance = {
                        "submission": "accepted",
                        "accepted": True,
                        "retry": False,
                        "status": accepted_status,
                        "message": event.get("message", ""),
                    }
                    idle_status = update_agent_status(session_id, {"active": False})
                    await ws_broadcast(
                        session_id,
                        {"type": "agent_status", "session_id": session_id, "status": idle_status},
                    )
                    yield chat_completion_chunk(chunk_id, created, model, {"st_proxy": acceptance})
                    yield chat_completion_chunk(chunk_id, created, model, {}, "stop")
                    yield "data: [DONE]\n\n"
                    return

                elif event_type == "done":
                    status = event.get("status", "complete")
                    payload = event.get("payload") or {}
                    if isinstance(payload.get("usage"), dict):
                        _update_session_info(session_id, usage=payload.get("usage"))
                        await ws_broadcast(
                            session_id,
                            {
                                "type": "session_info",
                                "session_id": session_id,
                                "last_usage": payload.get("usage"),
                            },
                        )
                        # Clear streaming estimate on final usage
                        _schedule_background_task(
                            ws_broadcast(
                                session_id,
                                {
                                    "type": "usage_update",
                                    "session_id": session_id,
                                    "streaming": False,
                                },
                            )
                        )
                    idle_status = update_agent_status(session_id, {"active": False})
                    await ws_broadcast(
                        session_id,
                        {"type": "agent_status", "session_id": session_id, "status": idle_status},
                    )
                    content_text, reasoning_delta = inline_reasoning.flush()
                    if reasoning_delta:
                        reasoning_text = reasoning_spacing.push(reasoning_delta)
                        if reasoning_text:
                            yield chat_completion_chunk(chunk_id, created, model, {"reasoning": reasoning_text})
                    if content_text:
                        text_emitted = True
                        if text_delta_callback is not None:
                            await text_delta_callback(content_text)
                        yield chat_completion_chunk(chunk_id, created, model, {"content": content_text})
                    remaining = reasoning_spacing.flush_remaining()
                    if remaining:
                        yield chat_completion_chunk(chunk_id, created, model, {"reasoning": remaining})
                    finish_reason = "stop" if status in {"complete", "interrupted"} else "error"
                    yield chat_completion_chunk(chunk_id, created, model, {}, finish_reason)
                    yield "data: [DONE]\n\n"
                    return

                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error from Hermes")
                    idle_status = update_agent_status(session_id, {"active": False})
                    await ws_broadcast(
                        session_id,
                        {"type": "agent_status", "session_id": session_id, "status": idle_status},
                    )
                    yield chat_completion_chunk(chunk_id, created, model, {"content": f"[Hermes Error] {error_msg}"})
                    yield chat_completion_chunk(chunk_id, created, model, {}, "error")
                    yield "data: [DONE]\n\n"
                    return

            if not text_emitted:
                logger.warning("Hermes stream ended without text for session %s", session_id)
            idle_status = update_agent_status(session_id, {"active": False})
            await ws_broadcast(
                session_id,
                {"type": "agent_status", "session_id": session_id, "status": idle_status},
            )
            content_text, reasoning_delta = inline_reasoning.flush()
            if reasoning_delta:
                reasoning_text = reasoning_spacing.push(reasoning_delta)
                if reasoning_text:
                    yield chat_completion_chunk(chunk_id, created, model, {"reasoning": reasoning_text})
            if content_text:
                text_emitted = True
                if text_delta_callback is not None:
                    await text_delta_callback(content_text)
                yield chat_completion_chunk(chunk_id, created, model, {"content": content_text})
            remaining = reasoning_spacing.flush_remaining()
            if remaining:
                yield chat_completion_chunk(chunk_id, created, model, {"reasoning": remaining})
            yield chat_completion_chunk(chunk_id, created, model, {}, "stop")
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.warning("Hermes stream cancelled for session %s (client disconnected)", session_id)
            if client_cancel_cleanup_task is None:
                async def finalize_stream_tool_calls() -> None:
                    await finalize_running_tool_calls_for_stream(session_id, stream_tool_call_ids)

                client_cancel_cleanup_task = asyncio.create_task(
                    _finish_client_cancel_cleanup(
                        hermes_ws_manager,
                        session_id,
                        on_interrupt_confirmed=finalize_stream_tool_calls,
                    )
                )
                client_cancel_cleanup_task.add_done_callback(_consume_task_result)
            try:
                await _await_task_despite_cancellation(client_cancel_cleanup_task)
            except asyncio.CancelledError:
                logger.warning("Hermes client cancellation cleanup was cancelled")
            except Exception as exc:
                logger.warning(
                    "Hermes client cancellation cleanup failed; error_type=%s",
                    type(exc).__name__,
                )
            # The HTTP peer is already gone. Do not attempt to write SSE data.
            raise
        except httpx.ReadTimeout:
            logger.error("Hermes stream idle timeout for session %s", session_id)
            idle_status = update_agent_status(session_id, {"active": False})
            await ws_broadcast(
                session_id,
                {"type": "agent_status", "session_id": session_id, "status": idle_status},
            )
            yield chat_completion_chunk(
                chunk_id,
                created,
                model,
                {"content": "[Hermes] Stream timed out - no events were received in time."},
            )
            yield chat_completion_chunk(chunk_id, created, model, {}, "error")
            yield "data: [DONE]\n\n"
        except Exception:
            logger.exception("Hermes stream error for session %s", session_id)
            idle_status = update_agent_status(session_id, {"active": False})
            await ws_broadcast(
                session_id,
                {"type": "agent_status", "session_id": session_id, "status": idle_status},
            )
            yield chat_completion_chunk(chunk_id, created, model, {"content": "[Hermes] Internal error during streaming"})
            yield chat_completion_chunk(chunk_id, created, model, {}, "error")
            yield "data: [DONE]\n\n"
        finally:
            if hermes_stream is not None:
                close_task = asyncio.ensure_future(hermes_stream.aclose())
                close_task.add_done_callback(_consume_task_result)
                try:
                    await _await_task_despite_cancellation(close_task)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning(
                        "Hermes stream cleanup failed; error_type=%s",
                        type(exc).__name__,
                    )
            if request_token is not None:
                reset_request_id(request_token)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def forward_to_backend(entry: dict[str, Any], body: Any, hermes_ws_manager: Any) -> Response | None:
    summary = entry["summary"]
    backend_name = configured_backend_name(summary["backend"])
    if not backend_name:
        if summary["backend"] not in {"dummy", "sillytavern"}:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Unsupported backend; Hermes Agent is the only runtime backend", "type": "invalid_request_error", "code": "unsupported_backend"}},
            )
        return None

    if backend_name == "hermes":
        undo_reason = hermes_undo_before_submit_reason(body)
        if undo_reason:
            session_id = summary["session_id"]
            try:
                result = await hermes_ws_manager.undo_session(session_id)
                logger.info(
                    "Hermes pre-submit undo request_id=%s session=%s reason=%s result=%s",
                    entry["request_id"],
                    session_id,
                    undo_reason,
                    result,
                )
            except ValueError as exc:
                if "No active Hermes session" in str(exc):
                    logger.info(
                        "Hermes pre-submit undo skipped request_id=%s session=%s reason=%s: %s",
                        entry["request_id"],
                        session_id,
                        undo_reason,
                        exc,
                    )
                else:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": {
                                "message": f"Hermes sync failed before generation: {exc}",
                                "type": "proxy_error",
                                "code": "hermes_sync_failed",
                                "request_id": entry["request_id"],
                            }
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "Hermes pre-submit undo failed request_id=%s session=%s reason=%s: %s",
                    entry["request_id"],
                    summary["session_id"],
                    undo_reason,
                    exc,
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "message": f"Hermes sync failed before generation: {exc}",
                            "type": "proxy_error",
                            "code": "hermes_sync_failed",
                            "request_id": entry["request_id"],
                        }
                    },
                )
        return await forward_hermes_streaming(
            body,
            summary["session_id"],
            hermes_ws_manager,
            entry["request_id"],
            sync_reason=undo_reason,
        )

    return None
