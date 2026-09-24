"""Server-side policy for selecting Hermes profiles."""

import re
from typing import Any

from . import config

DEFAULT_HERMES_PROFILE = "default"
PROFILE_NOT_ALLOWED_MESSAGE = "Hermes profile is not allowed"
PROFILE_NOT_ALLOWED_CODE = "profile_not_allowed"
_PROFILE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class ProfileNotAllowedError(ValueError):
    """A requested profile is outside the configured server allowlist."""

    def __init__(self) -> None:
        super().__init__(PROFILE_NOT_ALLOWED_MESSAGE)


def profile_allowlist() -> frozenset[str] | None:
    """Return the optional allowlist; ``None`` preserves private defaults."""
    return config.HERMES_PROFILE_ALLOWLIST


def is_profile_allowed(value: Any) -> bool:
    """Check a stored or listed profile without disclosing its name."""
    allowed = profile_allowlist()
    if allowed is None:
        return True
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return bool(normalized and _PROFILE_NAME_RE.fullmatch(normalized) and normalized in allowed)


def resolve_profile_selection(value: Any) -> str | None:
    """Validate an explicit selection and return its normalized profile name."""
    allowed = profile_allowlist()
    if allowed is None:
        return value if isinstance(value, str) else None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ProfileNotAllowedError()
    normalized = value.strip().lower()
    if not _PROFILE_NAME_RE.fullmatch(normalized) or normalized not in allowed:
        raise ProfileNotAllowedError()
    return normalized


def session_profile_override(value: str | None) -> str | None:
    """Pass the selected profile explicitly so Hermes cannot use a launch profile."""
    return value


def stored_profile_selection(value: Any) -> str | None:
    """Return an allowed persisted override, dropping profiles revoked later."""
    if profile_allowlist() is None:
        return value if isinstance(value, str) and value else None
    if not isinstance(value, str) or not is_profile_allowed(value):
        return None
    return value.strip().lower()


def profile_options_for_client(options: Any) -> dict[str, Any] | None:
    """Filter Hermes's profile response before it reaches a client selector."""
    allowed = profile_allowlist()
    if allowed is None:
        return options if isinstance(options, dict) else None
    if not isinstance(options, dict):
        return None

    raw_profiles = options.get("profiles")
    profiles = []
    if isinstance(raw_profiles, list):
        for item in raw_profiles:
            if not isinstance(item, dict) or not is_profile_allowed(item.get("name")):
                continue
            profiles.append({"name": str(item["name"]).strip().lower()})
    if DEFAULT_HERMES_PROFILE in allowed and not any(
        str(item.get("name", "")).strip().lower() == DEFAULT_HERMES_PROFILE
        for item in profiles
    ):
        profiles.insert(0, {"name": DEFAULT_HERMES_PROFILE})

    active = options.get("active")
    if not is_profile_allowed(active):
        active = DEFAULT_HERMES_PROFILE if DEFAULT_HERMES_PROFILE in allowed else None
    elif isinstance(active, str):
        active = active.strip().lower()
    return {"active": active, "profiles": profiles}


def profile_not_allowed_body() -> dict[str, dict[str, str]]:
    """Stable public error body shared by HTTP entry points."""
    return {
        "error": {
            "message": PROFILE_NOT_ALLOWED_MESSAGE,
            "type": "profile_error",
            "code": PROFILE_NOT_ALLOWED_CODE,
        }
    }
