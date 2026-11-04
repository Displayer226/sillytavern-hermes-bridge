#!/usr/bin/env python3
"""Check the public Hermes checkout and advertised revision without Docker."""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout.strip()


def main() -> int:
    try:
        entry = git("ls-files", "--stage", "--", "hermes-agent")
        match = re.fullmatch(r"160000 ([0-9a-f]{40}) 0\thermes-agent", entry)
        if not match:
            raise ValueError("Hermes gitlink is missing or conflicted")
        revision = match.group(1)
        if git("-C", "hermes-agent", "rev-parse", "HEAD") != revision:
            raise ValueError("Hermes checkout does not match the gitlink")
        if git("-C", "hermes-agent", "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("Hermes checkout has tracked or untracked changes")
        compose = (ROOT / "compose.public.yaml").read_text(encoding="utf-8")
        guide = (ROOT / "docs/public-docker-quickstart.md").read_text(encoding="utf-8")
        if f"image: sillytavern-hermes/hermes-agent:{revision[:8]}" not in compose:
            raise ValueError("Compose image tag does not match the gitlink")
        if f"# Expected: {revision}" not in guide:
            raise ValueError("Quick Start revision does not match the gitlink")
        print(f"Hermes gitlink, clean checkout, image tag and Quick Start agree: {revision}")
        return 0
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"Hermes pin check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
