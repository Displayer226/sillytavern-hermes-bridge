"""Offline checks for the public Compose contract."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    with tempfile.TemporaryDirectory() as home:
        return subprocess.run(
            args, cwd=ROOT, check=True, text=True, capture_output=True,
            env={"PATH": os.environ.get("PATH", os.defpath), "HOME": home,
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )


class PublicComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = command(
            "docker", "compose", "--env-file", ".env.public.example",
            "-f", "compose.public.yaml", "config", "--format", "json",
        )
        cls.services = json.loads(result.stdout)["services"]

    def test_source_provenance_check(self):
        command("python3", "scripts/check-public-hermes-pin.py")

    def test_password_basic_and_isolation(self):
        proxy = self.services["proxy"]
        env = proxy["environment"]
        self.assertEqual(env["HERMES_DASHBOARD_AUTH_MODE"], "password")
        self.assertEqual(env["HERMES_DASHBOARD_AUTH_PROVIDER"], "basic")
        self.assertEqual(env["HERMES_PROFILE_ALLOWLIST"], "default")
        self.assertEqual(env["PROXY_WS_QUERY_TOKEN_COMPAT"], "false")
        self.assertTrue(env["WS_TOKEN"])
        hermes_env = self.services["hermes"]["environment"]
        self.assertEqual(env["HERMES_DASHBOARD_AUTH_USERNAME"], hermes_env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"])
        self.assertEqual(env["HERMES_DASHBOARD_AUTH_PASSWORD"], hermes_env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"])
        self.assertFalse(any(k.startswith("HERMES_DASHBOARD_OIDC_") for k in env))
        self.assertEqual(set(proxy["networks"]), {"backend"})
        self.assertNotIn("ports", proxy)
        self.assertEqual({v["target"] for v in proxy["volumes"]}, {"/app/data", "/app/logs"})
        self.assertEqual(hermes_env["HERMES_TUI_TOOLSETS"], "context_engine")
        self.assertEqual(self.services["proxy-ingress"]["ports"][0]["host_ip"], "127.0.0.1")

    def test_liveness_drives_startup_not_backend_readiness(self):
        proxy = self.services["proxy"]
        ingress = self.services["proxy-ingress"]
        self.assertEqual(proxy["depends_on"]["hermes"]["condition"], "service_started")
        self.assertEqual(ingress["depends_on"]["proxy"]["condition"], "service_healthy")
        self.assertIn("/health/live", " ".join(proxy["healthcheck"]["test"]))
        self.assertIn("/health/live", " ".join(ingress["healthcheck"]["test"]))


if __name__ == "__main__":
    unittest.main()
