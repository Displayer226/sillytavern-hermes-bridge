#!/usr/bin/env python3
"""Check the public Hermes checkout and advertised revision without Docker."""

from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    env = os.environ.copy()
    if args[:2] == ("-C", "hermes-agent"):
        env.pop("GIT_INDEX_FILE", None)
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, text=True, capture_output=True, env=env
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
        provenance = (ROOT / "docs/export-provenance.md").read_text(encoding="utf-8")
        if f"image: sillytavern-hermes/hermes-agent:{revision[:8]}" not in compose:
            raise ValueError("Compose image tag does not match the gitlink")
        if f"# Expected: {revision}" not in guide:
            raise ValueError("Quick Start revision does not match the gitlink")
        fork = git("config", "-f", ".gitmodules", "--get", "submodule.hermes-agent.url")
        documented_revision = re.findall(r"^Current Hermes gitlink: `([^`]+)`$", provenance, re.MULTILINE)
        documented_fork = re.findall(r"^Current Hermes fork: `([^`]+)`$", provenance, re.MULTILINE)
        if documented_revision != [revision] or documented_fork != [fork]:
            raise ValueError("Current provenance revision or fork does not match the gitlink and .gitmodules")
        print(f"Hermes gitlink, clean checkout, image tag, Quick Start and provenance agree: {revision}")
        return 0
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f"Hermes pin check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
