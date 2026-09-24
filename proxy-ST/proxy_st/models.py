import asyncio
import json
import time
from typing import Any

import httpx

from .config import BACKEND_CONFIGS, MODELS, MODELS_CACHE_TTL_SECONDS, MODELS_FETCH_BACKENDS, MODELS_FETCH_TIMEOUT_SECONDS
from .log import logger
from .request_transform import backend_auth_headers, backend_endpoint


_models_cache: dict[str, Any] = {"data": None, "expires_at": 0.0, "created_at": 0.0}


def _models_cache_is_valid() -> bool:
    if _models_cache["data"] is None:
        return False
    return time.time() < _models_cache["expires_at"]


def _models_cache_set(data: list[dict[str, Any]]) -> None:
    _models_cache["data"] = data
    _models_cache["created_at"] = time.time()
    _models_cache["expires_at"] = _models_cache["created_at"] + MODELS_CACHE_TTL_SECONDS


def _models_cache_get() -> list[dict[str, Any]] | None:
    if _models_cache_is_valid():
        return _models_cache["data"]
    return None


def models_cache_age() -> float | None:
    if _models_cache["data"] is None or not _models_cache["created_at"]:
        return None
    return max(0.0, time.time() - float(_models_cache["created_at"]))


def clear_models_cache() -> None:
    _models_cache["data"] = None
    _models_cache["created_at"] = 0.0
    _models_cache["expires_at"] = 0.0


def model_object(model_id: str, owned_by: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": owned_by,
    }


def normalize_backend_model_item(item: Any, backend_name: str) -> dict[str, Any] | None:
    if isinstance(item, str):
        return model_object(item, backend_name)
    if not isinstance(item, dict):
        return None

    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None

    normalized = dict(item)
    normalized["id"] = model_id.strip()
    normalized.setdefault("object", "model")
    normalized.setdefault("created", 0)
    normalized.setdefault("owned_by", backend_name)
    return normalized


async def fetch_backend_models(backend_name: str, backend_config: dict[str, str]) -> list[dict[str, Any]]:
    if not backend_config.get("base_url"):
        return []

    url = backend_endpoint(backend_config["base_url"], "/models")
    try:
        async with httpx.AsyncClient(timeout=MODELS_FETCH_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=backend_auth_headers(backend_config))
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("backend models fetch failed backend=%s url=%s error=%s", backend_name, url, exc)
        return []

    raw_models = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        logger.warning("backend models fetch returned unexpected shape backend=%s url=%s", backend_name, url)
        return []

    models = []
    for item in raw_models:
        normalized = normalize_backend_model_item(item, backend_name)
        if normalized:
            models.append(normalized)
    return models


async def list_proxy_models(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        clear_models_cache()

    cached = _models_cache_get()
    cache_hit = cached is not None
    if cached is not None:
        logger.debug("returning models from cache (TTL=%ds)", MODELS_CACHE_TTL_SECONDS)
        return {
            "data": cached,
            "cache_hit": True,
            "cache_age": round(models_cache_age() or 0.0, 3),
            "cache_ttl": MODELS_CACHE_TTL_SECONDS,
        }

    logger.info("cache miss - fetching models from backends")
    models_by_id: dict[str, dict[str, Any]] = {
        model: model_object(model, "proxy-ST") for model in MODELS
    }

    if MODELS_FETCH_BACKENDS:
        tasks = [
            fetch_backend_models(name, config)
            for name, config in BACKEND_CONFIGS.items()
            if config.get("base_url")
        ]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                logger.warning("backend models fetch task failed: %s", result)
                continue
            for model in result:
                models_by_id[model["id"]] = model

    result = list(models_by_id.values())
    _models_cache_set(result)
    logger.info("models cache updated with %d model(s)", len(result))
    return {
        "data": result,
        "cache_hit": cache_hit,
        "cache_age": 0.0,
        "cache_ttl": MODELS_CACHE_TTL_SECONDS,
    }
