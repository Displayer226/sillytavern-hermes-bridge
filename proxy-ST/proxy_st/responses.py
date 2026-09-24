import json
import re
from typing import Any


INLINE_REASONING_OPEN_RE = re.compile(
    r"<(?:think|thinking|reasoning|thought)>|<\|channel\>\s*(?:thought|thinking|reasoning|analysis)\b",
    re.IGNORECASE,
)
INLINE_REASONING_OPEN_PREFIXES = (
    "<think>",
    "<thinking>",
    "<reasoning>",
    "<thought>",
    "<|channel>thought",
    "<|channel>thinking",
    "<|channel>reasoning",
    "<|channel>analysis",
)
INLINE_REASONING_CLOSE_TAGS = ("</think>", "</thinking>", "</reasoning>", "</thought>", "<channel|>")
_NO_SPACE_BEFORE_REASONING_CHARS = set(".,;:!?)]}")
_NO_SPACE_AFTER_REASONING_CHARS = set("([{")
_SPACE_BEFORE_REASONING_DASH_AFTER_CHARS = set(".!?;:)]}\"'")


def _is_reasoning_word_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or char == "_")


def _needs_reasoning_boundary_space(
    previous: str,
    current: str,
    chunk: str,
    double_quote_open: bool = False,
    previous_dash_spaced: bool = False,
) -> bool:
    if not previous or not current:
        return False
    if previous.isspace() or current.isspace():
        return False
    if current in _NO_SPACE_BEFORE_REASONING_CHARS:
        return False
    if previous in _NO_SPACE_AFTER_REASONING_CHARS:
        return False

    stripped_chunk = chunk.strip()
    if current == "'":
        return False
    if current == '"':
        if double_quote_open:
            return False
        return _is_reasoning_word_char(previous) or previous in ".!?;:"
    if current == "-":
        return stripped_chunk in {"-", "--"} and previous in _SPACE_BEFORE_REASONING_DASH_AFTER_CHARS
    if previous == "-":
        return previous_dash_spaced and (_is_reasoning_word_char(current) or current == '"')
    if _is_reasoning_word_char(previous) and _is_reasoning_word_char(current):
        return True
    if previous in ".!?;:" and (_is_reasoning_word_char(current) or current == '"'):
        return True
    return False


class ReasoningSpacingNormalizer:
    """Restore readable spacing when reasoning arrives as bare word deltas."""

    def __init__(self) -> None:
        self._last_char = ""
        self._double_quote_open = False
        self._last_dash_was_spaced = False
        self._buffer: list[str] = []
        self._has_seen_native_spaces = False
        self._determined = False
        self._buffer_limit = 3

    def push(self, text: Any) -> str:
        chunk = str(text or "")
        if not chunk:
            return ""

        if self._determined:
            return self._process_chunk(chunk)

        self._buffer.append(chunk)

        # Check if we can determine the stream type
        if any(" " in c or "\n" in c or "\t" in c for c in self._buffer):
            self._has_seen_native_spaces = True
            self._determined = True
            flushed = "".join(self._buffer)
            self._buffer.clear()
            if flushed:
                self._last_char = flushed[-1]
                for char in flushed:
                    if char == '"':
                        self._double_quote_open = not self._double_quote_open
            return flushed

        if len(self._buffer) >= self._buffer_limit:
            self._has_seen_native_spaces = False
            self._determined = True
            flushed_parts = []
            for c in self._buffer:
                flushed_parts.append(self._process_chunk(c))
            self._buffer.clear()
            return "".join(flushed_parts)

        return ""

    def _process_chunk(self, chunk: str) -> str:
        prefix = ""
        if not self._has_seen_native_spaces:
            if _needs_reasoning_boundary_space(
                self._last_char,
                chunk[0],
                chunk,
                self._double_quote_open,
                self._last_dash_was_spaced,
            ):
                prefix = " "

        normalized = f"{prefix}{chunk}"
        self._last_char = normalized[-1]
        self._last_dash_was_spaced = bool(prefix and chunk.strip() in {"-", "--"} and chunk[0] == "-")
        for char in chunk:
            if char == '"':
                self._double_quote_open = not self._double_quote_open
        return normalized

    def flush_remaining(self) -> str:
        if self._determined:
            return ""
        self._has_seen_native_spaces = False
        self._determined = True
        flushed_parts = []
        for c in self._buffer:
            flushed_parts.append(self._process_chunk(c))
        self._buffer.clear()
        return "".join(flushed_parts)


class InlineReasoningStreamSplitter:
    """Split inline reasoning blocks out of streamed visible text."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_reasoning = False
        self._close_tag = ""

    def push(self, text: Any) -> tuple[str, str]:
        chunk = str(text or "")
        if not chunk:
            return "", ""

        self._buffer += chunk
        return self._drain(final=False)

    def flush(self) -> tuple[str, str]:
        return self._drain(final=True)

    def _drain(self, final: bool) -> tuple[str, str]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []

        while self._buffer:
            if self._in_reasoning:
                close_index = self._buffer.lower().find(self._close_tag)
                if close_index >= 0:
                    reasoning_parts.append(self._buffer[:close_index])
                    self._buffer = self._buffer[close_index + len(self._close_tag):]
                    self._in_reasoning = False
                    self._close_tag = ""
                    continue

                if final:
                    reasoning_parts.append(self._buffer)
                    self._buffer = ""
                    self._in_reasoning = False
                    self._close_tag = ""
                    break

                keep = max(len(tag) for tag in INLINE_REASONING_CLOSE_TAGS) - 1
                if len(self._buffer) <= keep:
                    break
                reasoning_parts.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
                break

            match = INLINE_REASONING_OPEN_RE.search(self._buffer)
            if match:
                content_parts.append(self._buffer[:match.start()])
                opener = match.group(0).lower()
                self._close_tag = "<channel|>" if opener.startswith("<|channel>") else "</" + opener[1:]
                self._buffer = self._buffer[match.end():]
                self._in_reasoning = True
                continue

            if final:
                content_parts.append(self._buffer)
                self._buffer = ""
                break

            keep = max(len(prefix) for prefix in INLINE_REASONING_OPEN_PREFIXES) - 1
            if len(self._buffer) <= keep:
                break
            content_parts.append(self._buffer[:-keep])
            self._buffer = self._buffer[-keep:]
            break

        return "".join(content_parts), "".join(reasoning_parts)


def responses_usage_to_chat_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def sse_chunk(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def chat_completion_chunk(
    chunk_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> str:
    return sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )
