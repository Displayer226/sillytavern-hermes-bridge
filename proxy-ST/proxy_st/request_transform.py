import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .config import (
    BACKEND_BODY_CANDIDATES,
    BACKEND_CONFIGS,
    DEFAULT_BACKEND,
    SESSION_BODY_CANDIDATES,
    SESSION_BODY_OBJECT_CANDIDATES,
    SESSION_BODY_OBJECT_KEY_CANDIDATES,
    SESSION_HEADER_CANDIDATES,
)
from .profile_policy import profile_allowlist, resolve_profile_selection
from .utils import has_unresolved_macro, stable_body_fingerprint, usable_metadata_value


SESSION_MARKER_RE = re.compile(r"<ST_PROXY_SESSION\b(?P<attrs>[^>]*)/?>", re.IGNORECASE)
ATTR_RE = re.compile(r"(?P<key>[\w:-]+)\s*=\s*(['\"])(?P<value>.*?)\2")
LEADING_REASONING_BLOCK_RE = re.compile(
    r"^\s*(?:"
    r"<(?:think|thinking|reasoning|thought)>\s*.*?\s*</(?:think|thinking|reasoning|thought)>"
    r"|<\|channel\>\s*(?:thought|thinking|reasoning|analysis)\b\s*.*?\s*<channel\|>"
    r")\s*",
    re.IGNORECASE | re.DOTALL,
)
TOOL_CALL_BLOCK_RE = re.compile(
    r"<(?:tool_call|tool_calls|tool_result|function_call|function_calls)\b[^>]*>.*?</(?:tool_call|tool_calls|tool_result|function_call|function_calls)>",
    re.IGNORECASE | re.DOTALL,
)
DATA_IMAGE_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)
DATA_IMAGE_URL_RE = re.compile(
    r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)
MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\((?P<url>data:image/[a-zA-Z0-9.+-]+;base64,[^)]+|https?://[^)\s]+)\)",
    re.IGNORECASE,
)
BARE_IMAGE_URL_RE = re.compile(
    r"https?://[^\s)\"']+\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?[^\s)\"']*)?",
    re.IGNORECASE,
)
HERMES_INLINE_CONTEXT_MAX_CHARS = 6000


@dataclass(frozen=True)
class HermesPromptPayload:
    text: str
    system_context: str | None = None
    conversation_history: list[dict[str, str]] | None = None
    persona_context: str | None = None
    persona_reminder: str | None = None
    persona_version: str | None = None
    mode: str = "full_context"


def _normalize_image_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    normalized = url.strip()
    if not normalized:
        return None
    if normalized.lower().startswith("data:image/"):
        return "".join(normalized.split())
    if normalized.startswith(("http://", "https://")):
        return normalized
    return None


def _image_references(text: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for regex in (MARKDOWN_IMAGE_RE, DATA_IMAGE_RE, BARE_IMAGE_URL_RE):
        for match in regex.finditer(text):
            url = _normalize_image_url(match.groupdict().get("url") or match.group(0))
            if url:
                matches.append((match.start(), match.end(), url))

    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    non_overlapping: list[tuple[int, int, str]] = []
    cursor = -1
    for start, end, url in matches:
        if start < cursor:
            continue
        non_overlapping.append((start, end, url))
        cursor = end
    return non_overlapping


def strip_image_references_from_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    for start, end, _ in reversed(_image_references(text)):
        cleaned = cleaned[:start] + cleaned[end:]
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return strip_image_references_from_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(strip_image_references_from_text(item))
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(strip_image_references_from_text(text))
        return "\n".join(parts)
    return ""


def _append_input_text(parts: list[dict[str, str]], text: str) -> None:
    cleaned = text.strip()
    if cleaned:
        parts.append({"type": "input_text", "text": cleaned})


def _append_input_image(parts: list[dict[str, str]], url: Any) -> None:
    normalized = _normalize_image_url(url)
    if normalized:
        parts.append({"type": "input_image", "image_url": normalized})


def _image_extension_from_mime(mime_type: str) -> str:
    normalized = mime_type.lower().split(";", 1)[0].strip()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
    }.get(normalized, ".png")


def _hermes_image_attachment_from_url(url: Any, index: int) -> dict[str, str] | None:
    normalized = _normalize_image_url(url)
    if not normalized:
        return None
    match = DATA_IMAGE_URL_RE.match(normalized)
    if not match:
        return None
    mime_type = match.group("mime").lower()
    return {
        "content_base64": normalized,
        "filename": f"sillytavern_image_{index}{_image_extension_from_mime(mime_type)}",
        "mime_type": mime_type,
    }


def _content_parts_from_string(text: str) -> list[dict[str, str]]:
    parts: list[dict[str, str]] = []
    last_index = 0
    for start, end, url in _image_references(text):
        _append_input_text(parts, text[last_index:start])
        _append_input_image(parts, url)
        last_index = end
    _append_input_text(parts, text[last_index:])
    return parts


def responses_content_parts(content: Any) -> list[dict[str, str]]:
    if isinstance(content, str):
        return _content_parts_from_string(content)

    parts: list[dict[str, str]] = []
    if not isinstance(content, list):
        return parts

    for item in content:
        if isinstance(item, str):
            parts.extend(_content_parts_from_string(item))
            continue
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type") or "").lower()
        if item_type in {"text", "input_text"}:
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.extend(_content_parts_from_string(text))
            continue

        image_url = item.get("image_url")
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        if image_url is None:
            image_url = item.get("url")
        if image_url is None and item.get("base64"):
            mime_type = item.get("mime_type") or item.get("media_type") or "image/png"
            image_url = f"data:{mime_type};base64,{item.get('base64')}"
        if item_type in {"image_url", "input_image", "image"} or image_url:
            _append_input_image(parts, image_url)

    return parts


def request_messages(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    messages = body.get("messages")
    return messages if isinstance(messages, list) else []


def extract_session_marker(body: Any) -> dict[str, str] | None:
    for message in request_messages(body):
        if not isinstance(message, dict):
            continue
        content = message_content_text(message.get("content"))
        match = SESSION_MARKER_RE.search(content)
        if not match:
            continue
        attrs = {
            attr_match.group("key").lower(): attr_match.group("value")
            for attr_match in ATTR_RE.finditer(match.group("attrs"))
        }
        attrs["raw_marker"] = match.group(0)
        return attrs
    return None


def extract_proxy_metadata(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}

    metadata: dict[str, Any] = {}
    for key in SESSION_BODY_OBJECT_CANDIDATES:
        value = body.get(key)
        if isinstance(value, dict):
            metadata.update(value)

    for key in (*SESSION_BODY_CANDIDATES, *BACKEND_BODY_CANDIDATES):
        if key in body:
            metadata[key] = body[key]

    return metadata


def detect_backend(model: str | None, headers: dict[str, str], body: Any) -> str:
    metadata = extract_proxy_metadata(body)
    body_backend = usable_metadata_value(metadata.get("backend"))
    if body_backend:
        return body_backend.lower()

    for key in BACKEND_BODY_CANDIDATES:
        body_backend = usable_metadata_value(metadata.get(key))
        if body_backend:
            return body_backend.lower()

    explicit = headers.get("x-st-backend") or headers.get("x-backend")
    if explicit and not has_unresolved_macro(explicit):
        return explicit.lower()
    if model:
        normalized_model = model.strip().lower()
        if normalized_model.endswith("-log"):
            return "dummy"
        known_backends = {*BACKEND_CONFIGS.keys(), "sillytavern"}
        for backend in known_backends:
            if normalized_model == backend or normalized_model.startswith((f"{backend}/", f"{backend}:", f"{backend}-")):
                return backend
        prefix = model.split(":", 1)[0].split("-", 1)[0].lower()
        if prefix in known_backends:
            return prefix
    return "unknown"


def proxy_workspace_override(body: Any) -> str | None:
    """Return a relative workspace choice carried in st_proxy metadata."""
    metadata = extract_proxy_metadata(body)
    value = usable_metadata_value(metadata.get("workspace"))
    if not value:
        return None
    # The client submits a relative path; reject absolute/traversal values here
    # before the workspace resolver performs its root-constrained canonicalisation.
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/"):
        return None
    normalized = raw.strip("/")
    if not normalized or normalized == "." or ".." in normalized.split("/"):
        return None
    return normalized


def proxy_profile_override(body: Any) -> str | None:
    """Return a safe Hermes profile name carried in st_proxy metadata."""
    metadata = extract_proxy_metadata(body)
    if profile_allowlist() is not None:
        if "profile" not in metadata:
            return None
        return resolve_profile_selection(metadata.get("profile"))

    value = usable_metadata_value(metadata.get("profile"))
    if not value:
        return None
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized):
        return None
    return normalized


def proxy_participant_name(body: Any, key: str) -> str | None:
    """Return a short, single-line participant name from st_proxy metadata."""
    if key not in {"user_name", "character_name"}:
        return None
    value = usable_metadata_value(extract_proxy_metadata(body).get(key))
    if not value:
        return None
    normalized = " ".join(value.split()).strip()
    return normalized[:120] or None


def detect_session(headers: dict[str, str], body: Any) -> tuple[str, str]:
    for header in SESSION_HEADER_CANDIDATES:
        value = headers.get(header)
        if value and not has_unresolved_macro(value):
            return value, header

    metadata = extract_proxy_metadata(body)
    for key in SESSION_BODY_OBJECT_KEY_CANDIDATES:
        value = usable_metadata_value(metadata.get(key))
        if value:
            return value, f"body-st_proxy:{key}"

    for key in SESSION_BODY_CANDIDATES:
        value = usable_metadata_value(metadata.get(key))
        if value:
            return value, f"body:{key}"

    marker = extract_session_marker(body)
    if marker:
        for key in ("session", "session_id", "id", "chat", "chat_id"):
            value = marker.get(key)
            if value and not has_unresolved_macro(value):
                return value, f"body-marker:{key}"
        return f"marker-{stable_body_fingerprint(marker)}", "body-marker-fingerprint"

    model = body.get("model") if isinstance(body, dict) else None
    messages = request_messages(body)
    anchor: dict[str, Any] = {"model": model}

    if messages:
        # Use only the first user message as a stable anchor (not first_messages
        # which shifts as the conversation grows and fragments sessions).
        user_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        anchor["first_user_message"] = user_messages[0] if user_messages else None
        last_user = user_messages[-1] if user_messages else None
        if last_user:
            anchor["last_user_message"] = last_user

    return f"fallback-{stable_body_fingerprint(anchor)}", "fallback-fingerprint"


def configured_backend_name(requested_backend: str) -> str | None:
    requested = requested_backend.lower()
    if requested == "unknown":
        requested = DEFAULT_BACKEND
    if requested == "hermes" and BACKEND_CONFIGS["hermes"].get("ws_url"):
        return "hermes"
    return None


def backend_endpoint(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def proxy_model_override(body: Any, backend_name: str) -> str | None:
    metadata = extract_proxy_metadata(body)
    for key in (f"{backend_name}_model", "model"):
        value = usable_metadata_value(metadata.get(key))
        if value:
            return value
    return None


def hermes_undo_before_submit_reason(body: Any) -> str | None:
    if isinstance(body, dict):
        generation_type = str(body.get("type") or "").strip().lower()
        if generation_type in {"regenerate", "swipe"}:
            return generation_type

    metadata = extract_proxy_metadata(body)
    reason = usable_metadata_value(metadata.get("hermes_undo_before_submit"))
    if not reason:
        return None
    if reason.strip().lower() in {"0", "false", "no", "off"}:
        return None
    return reason


def strip_session_markers_from_content(content: Any) -> Any:
    if isinstance(content, str):
        return SESSION_MARKER_RE.sub("", content).strip()
    if isinstance(content, list):
        cleaned_items = []
        for item in content:
            if isinstance(item, str):
                cleaned_items.append(strip_session_markers_from_content(item))
            elif isinstance(item, dict):
                cleaned = dict(item)
                for key in ("text", "content"):
                    if key in cleaned:
                        cleaned[key] = strip_session_markers_from_content(cleaned[key])
                cleaned_items.append(cleaned)
            else:
                cleaned_items.append(item)
        return cleaned_items
    return content


def clean_request_messages(body: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for message in request_messages(body):
        if not isinstance(message, dict):
            continue
        cleaned_message = dict(message)
        cleaned_message["content"] = strip_session_markers_from_content(message.get("content"))
        messages.append(cleaned_message)
    return messages


def useful_message_text(message: dict[str, Any]) -> str:
    return message_content_text(message.get("content")).strip()


def visible_message_text(message: dict[str, Any]) -> str:
    text = useful_message_text(message)
    if str(message.get("role") or "").strip().lower() == "assistant":
        text = LEADING_REASONING_BLOCK_RE.sub("", text).strip()
        text = TOOL_CALL_BLOCK_RE.sub("", text).strip()
    return text


def is_new_chat_marker(text: str) -> bool:
    stripped = text.strip()
    return stripped in {"[Start a new Chat]", "[Start a new chat]"}


def has_new_chat_marker(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if is_new_chat_marker(useful_message_text(message)):
            return True
    return False


def response_input_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = useful_message_text(message)
        if text and not is_new_chat_marker(text):
            return text

    for message in reversed(messages):
        if message.get("role") in {"system", "developer"}:
            continue
        text = useful_message_text(message)
        if text and not is_new_chat_marker(text):
            return text

    return ""


def latest_user_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        if is_new_chat_marker(useful_message_text(message)):
            continue
        return message
    return None


def latest_current_turn_user_message_with_images(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    start_index = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() == "assistant":
            start_index = index

    for message in reversed(messages[start_index + 1:]):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        if is_new_chat_marker(useful_message_text(message)):
            continue
        parts = responses_content_parts(message.get("content"))
        if any(part.get("type") == "input_image" for part in parts):
            return message
    return None


def hermes_image_attachments_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return image attachments from the current user turn for Hermes WS.

    Hermes sessions are stateful, so only the current user turn should queue
    images. Older image turns are already part of Hermes' session history.
    """
    selected = latest_current_turn_user_message_with_images(messages)
    if selected is None:
        return []

    attachments: list[dict[str, str]] = []
    for part in responses_content_parts(selected.get("content")):
        if part.get("type") != "input_image":
            continue
        attachment = _hermes_image_attachment_from_url(part.get("image_url"), len(attachments) + 1)
        if attachment:
            attachments.append(attachment)
    return attachments


def split_system_instructions_from_messages(messages: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    start_index = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if is_new_chat_marker(useful_message_text(message)):
            start_index = index

    pre_history_parts: list[str] = []
    post_history_parts: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") not in {"system", "developer"}:
            continue
        text = useful_message_text(message)
        if not text or is_new_chat_marker(text):
            continue

        if start_index >= 0 and index > start_index:
            post_history_parts.append(text)
        else:
            pre_history_parts.append(text)

    pre_history = "\n\n".join(pre_history_parts) if pre_history_parts else None
    post_history = "\n\n".join(post_history_parts) if post_history_parts else None
    return pre_history, post_history


def conversation_messages_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    start_index = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if is_new_chat_marker(useful_message_text(message)):
            start_index = index

    conversation: list[dict[str, str]] = []
    for message in messages[start_index + 1:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role in {"system", "developer"}:
            continue
        text = visible_message_text(message)
        if not text or is_new_chat_marker(text):
            continue
        conversation.append({"role": role or "message", "content": text})
    return conversation


def _message_has_hermes_user_payload(message: dict[str, Any]) -> bool:
    if useful_message_text(message):
        return True
    return any(part.get("type") == "input_image" for part in responses_content_parts(message.get("content")))


def latest_hermes_user_message_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        if is_new_chat_marker(useful_message_text(message)):
            continue
        if _message_has_hermes_user_payload(message):
            return index
    return None


def hermes_visible_history_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    latest_user_index = latest_hermes_user_message_index(messages)
    if latest_user_index is None:
        return conversation_messages_from_messages(messages)

    history: list[dict[str, str]] = []
    start_index = -1
    for index, message in enumerate(messages[:latest_user_index]):
        if not isinstance(message, dict):
            continue
        if is_new_chat_marker(useful_message_text(message)):
            start_index = index

    for message in messages[start_index + 1:latest_user_index]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role in {"system", "developer"}:
            continue
        if role not in {"user", "assistant"}:
            continue
        text = visible_message_text(message)
        if not text or is_new_chat_marker(text):
            continue
        history.append({"role": role, "content": text})
    return history


def hermes_system_context_from_messages(
    messages: list[dict[str, Any]],
    *,
    integration_context: str | None = None,
    include_persona: bool = True,
) -> str | None:
    instructions, _ = split_system_instructions_from_messages(messages)
    parts: list[str] = []
    if integration_context:
        parts.append(str(integration_context).strip())
    if include_persona and instructions:
        parts.append("[SillyTavern persona and instructions]\n" + instructions)
    parts.append(
        "[SillyTavern response contract]\n"
        "Treat the provided SillyTavern visible chat history as the authoritative current chat state. "
        "Continue the conversation from the current user message as the assistant/character. "
        "The assistant has already sent any assistant messages present in the visible chat history, including any opening message. "
        "Do not restart the chat, repeat the opening message, or greet again unless the latest user explicitly asks you to greet them. "
        "If you need a tool, use the native Hermes tool-calling mechanism and only call tools that are actually available in the Hermes tool list. "
        "Never print pseudo tool calls or XML/function-call text such as <tool_call>, web_search(...), calculator(...), or similar syntax in the visible reply. "
        "Write only the next assistant reply in the same language as the conversation."
    )
    context = "\n\n".join(part for part in parts if part)
    return context or None


def _truncate_inline_context(text: str, max_chars: int = HERMES_INLINE_CONTEXT_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    cutoff = text.rfind("\n\n", 0, max_chars)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return (
        text[:cutoff].rstrip()
        + "\n\n[...SillyTavern persona context truncated here; full context remains in persona_context...]"
    )


def hermes_persona_context_from_messages(messages: list[dict[str, Any]]) -> str | None:
    instructions, _ = split_system_instructions_from_messages(messages)
    return instructions or None


def hermes_persona_version(persona_context: str | None) -> str | None:
    if not persona_context:
        return None
    return hashlib.sha256(persona_context.encode("utf-8")).hexdigest()[:16]


def hermes_persona_reminder_from_context(persona_context: str | None) -> str | None:
    if not persona_context:
        return None

    return (
        "[SillyTavern persona reminder]\n"
        "This combined SillyTavern context may contain both the USER persona/profile and the "
        "ASSISTANT character card. Use only the assistant character/card as your visible identity, "
        "voice, relationship, and roleplay source. Never adopt the user's name, appearance, traits, "
        "biography, or abilities as your own. Hermes USER.md also describes the user, not the assistant. "
        "If the user asks for your name, character, style, relationship, or persona, answer from this "
        "block's assistant character/card; do not inspect Hermes config, project files, memory, or tools "
        "to discover it.\n\n"
        + _truncate_inline_context(persona_context, max_chars=1800)
        + "\n[/SillyTavern persona reminder]"
    )


def hermes_prompt_payload_from_messages_for_request(
    messages: list[dict[str, Any]],
    *,
    sync_reason: str | None = None,
    active_session: bool = False,
    integration_context: str | None = None,
) -> HermesPromptPayload:
    reason = str(sync_reason or "").strip().lower()
    use_delta = active_session and (not reason or reason in {"regenerate", "swipe"})

    text = response_input_from_messages(messages)
    _, post_history_instructions = split_system_instructions_from_messages(messages)
    if post_history_instructions:
        text = f"{text}\n\n[SillyTavern post-history instructions]\n{post_history_instructions}"

    persona_context = hermes_persona_context_from_messages(messages) or ""

    history = None if use_delta else hermes_visible_history_from_messages(messages)
    mode = "active_delta" if use_delta else "structured_context"
    return HermesPromptPayload(
        text=text,
        system_context=hermes_system_context_from_messages(
            messages,
            integration_context=integration_context,
            include_persona=False,
        ),
        conversation_history=history or None,
        persona_context=persona_context,
        persona_reminder=hermes_persona_reminder_from_context(persona_context) or "",
        persona_version=hermes_persona_version(persona_context) or "",
        mode=mode,
    )


def hermes_prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    instructions, post_history_instructions = split_system_instructions_from_messages(messages)
    conversation = conversation_messages_from_messages(messages)

    if not conversation:
        last_input = response_input_from_messages(messages)
        if not last_input:
            return ""
        conversation = [{"role": "user", "content": last_input}]

    parts: list[str] = []
    if instructions:
        parts.append("[SillyTavern persona and instructions]\n" + instructions)

    rendered_turns = "\n\n".join(
        f"{turn['role']}:\n{turn['content']}"
        for turn in conversation
    )
    parts.append("[SillyTavern conversation so far]\n" + rendered_turns)
    if post_history_instructions:
        parts.append("[SillyTavern post-history instructions]\n" + post_history_instructions)
    parts.append(
        "[Task]\n"
        "Treat the SillyTavern conversation above as the authoritative current chat state. "
        "Continue the conversation from the final user message as the assistant/character. "
        "The assistant has already sent the assistant messages listed above, including any opening message. "
        "Do not restart the chat, repeat the opening message, or greet again unless the latest user explicitly asks you to greet them. "
        "If you need a tool, use the native Hermes tool-calling mechanism and only call tools that are actually available in the Hermes tool list. "
        "Never print pseudo tool calls or XML/function-call text such as <tool_call>, web_search(...), calculator(...), or similar syntax in the visible reply. "
        "Write only the next assistant reply in the same language as the conversation."
    )
    return "\n\n".join(parts)


def hermes_prompt_from_messages_for_request(
    messages: list[dict[str, Any]],
    *,
    sync_reason: str | None = None,
    active_session: bool = False,
) -> str:
    """Choose the Hermes prompt shape for this submission.

    Hermes sessions are stateful. Once a SillyTavern chat is already mapped to
    an active Hermes session, replaying the full visible transcript on every
    request duplicates history inside Hermes and can force repeated compression.
    A fresh Hermes session still needs the full SillyTavern transcript to
    bootstrap context.
    """
    reason = str(sync_reason or "").strip().lower()
    if active_session and (not reason or reason in {"regenerate", "swipe"}):
        return response_input_from_messages(messages)
    return hermes_prompt_from_messages(messages)


def backend_auth_headers(backend_config: dict[str, str]) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    api_key = backend_config.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
