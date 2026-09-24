import time

from fastapi.responses import JSONResponse

from .config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW, RATE_LIMITING_ENABLED
from .log import logger
from .state import RATE_LIMIT_TRACKER


def _check_rate_limit(session_id: str) -> bool:
    if not RATE_LIMITING_ENABLED:
        return True

    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    if session_id in RATE_LIMIT_TRACKER:
        RATE_LIMIT_TRACKER[session_id] = [t for t in RATE_LIMIT_TRACKER[session_id] if t > window_start]
    else:
        RATE_LIMIT_TRACKER[session_id] = []

    if len(RATE_LIMIT_TRACKER[session_id]) >= RATE_LIMIT_REQUESTS:
        logger.warning(
            "Rate limit exceeded for session %s (%d requests in %ds)",
            session_id,
            len(RATE_LIMIT_TRACKER[session_id]),
            RATE_LIMIT_WINDOW,
        )
        return False

    RATE_LIMIT_TRACKER[session_id].append(now)
    return True


def _rate_limit_response(session_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": f"Rate limit exceeded: {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW}s",
                "type": "rate_limit_error",
                "param": None,
                "code": "rate_limit_exceeded",
            }
        },
    )
