#!/usr/bin/env python3
"""Reject missing or example credentials before any public Quick Start container starts."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED = (
    "WS_TOKEN",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    "CORS_ORIGINS",
)
PLACEHOLDER = re.compile(
    r"^(?:replace(?:[_ -]|$)|placeholder(?:[_ -]|$)|change[_ -]?me(?:$|[_ -])|"
    r"your[_ -]|todo$|example$|<[^>]+>|\$\{[^}]+\})",
    re.IGNORECASE,
)


def read_env(path: Path) -> tuple[dict[str, str], set[str]]:
    values: dict[str, str] = {}
    duplicates: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            # Compose treats a whitespace-prefixed # as an inline comment.
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if value.startswith("#"):
                value = ""
        if key in values:
            duplicates.add(key)
        values[key] = value
    return values, duplicates


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(".env.public")
    if len(sys.argv) > 2:
        print("FAIL: pass at most one env file path", file=sys.stderr)
        return 2
    try:
        values, duplicates = read_env(path)
    except OSError:
        print("FAIL: Quick Start env file is missing or unreadable", file=sys.stderr)
        return 1

    invalid = {
        key
        for key in REQUIRED
        if not values.get(key, "").strip() or PLACEHOLDER.match(values.get(key, "").strip())
    }
    invalid.update(duplicates)
    if invalid:
        print(
            f"FAIL: {len(invalid)} Quick Start setting(s) are empty, duplicated, or placeholders",
            file=sys.stderr,
        )
        return 1
    print("PASS: required Quick Start values are present and are not placeholders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
