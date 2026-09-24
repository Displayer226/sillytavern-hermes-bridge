import time
import uuid
from typing import AsyncIterator, Any

from .responses import sse_chunk


def chat_completion_response(model: str, content: str) -> dict[str, Any]:
    created = int(time.time())
    return {
        "id": f"chatcmpl-log-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def stream_response(model: str, content: str) -> AsyncIterator[str]:
    chunk_id = f"chatcmpl-log-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    yield sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )
    yield sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
    )
    yield sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    yield "data: [DONE]\n\n"
