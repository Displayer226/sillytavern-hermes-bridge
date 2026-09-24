import secrets

from fastapi import Request


PUBLIC_HTTP_PATHS = {"/health", "/health/live", "/health/ready", "/metrics"}


def is_public_http_path(path: str) -> bool:
    return path in PUBLIC_HTTP_PATHS


def is_worker_voice_path(path: str) -> bool:
    if not path.startswith("/v1/voice/calls/"):
        return False
    parts = path.strip("/").split("/")
    if len(parts) != 5:
        return False
    return parts[-1] in {"context", "error", "finish", "events", "completions"}


def request_has_proxy_token(request: Request, token: str) -> bool:
    if not token:
        return True

    expected = f"Bearer {token}"
    authorization = request.headers.get("authorization", "")
    if secrets.compare_digest(authorization, expected):
        return True

    header_token = request.headers.get("x-proxy-token", "")
    if header_token and secrets.compare_digest(header_token, token):
        return True

    query_token = request.query_params.get("token", "")
    return bool(query_token and secrets.compare_digest(query_token, token))
