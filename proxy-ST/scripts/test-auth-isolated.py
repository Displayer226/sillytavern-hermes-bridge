#!/usr/bin/env python3
"""Run focused auth tests with temporary state and network connections blocked."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bridge-auth-tests-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": tmp,
            "TMPDIR": tmp,
            "HERMES_HOME": f"{tmp}/hermes",
            "LOG_DIR": f"{tmp}/logs",
            "SESSIONS_DB_FILE": f"{tmp}/state/sessions.sqlite3",
            "SESSIONS_DATA_FILE": f"{tmp}/state/sessions.json",
            "HERMES_DASHBOARD_OIDC_TOKEN_FILE": f"{tmp}/state/oidc.json",
            "SESSION_PERSISTENCE_ENABLED": "false",
            "PROXY_FETCH_BACKEND_MODELS": "false",
            "WS_TOKEN": "synthetic-proxy-token",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "tests"))),
        }
        return subprocess.run([
            sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin",
            "-p", "auth_network_guard", "-p", "no:cacheprovider", "-q",
            "tests/test_auth_contract.py", "tests/test_hermes_auth.py",
            "tests/test_hermes_oidc.py", "tests/test_hermes_web_auth.py",
            "tests/test_auth.py", "tests/test_public_health_contract.py",
        ], cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
