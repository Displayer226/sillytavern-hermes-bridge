"""Pin checker rejects drift between the gitlink, checkout and public provenance."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pin_check", ROOT / "scripts/check-public-hermes-pin.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
REVISION = "a" * 40
URL = "https://example.invalid/hermes.git"


class PublicPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "compose.public.yaml").write_text(f"image: sillytavern-hermes/hermes-agent:{REVISION[:8]}\n")
        (self.root / "docs/public-docker-quickstart.md").write_text(f"# Expected: {REVISION}\n")
        (self.root / "docs/export-provenance.md").write_text(
            f"Current Hermes gitlink: `{REVISION}`\nCurrent Hermes fork: `{URL}`\n")
        self.responses = {
            ("ls-files", "--stage", "--", "hermes-agent"): f"160000 {REVISION} 0\thermes-agent",
            ("-C", "hermes-agent", "rev-parse", "HEAD"): REVISION,
            ("-C", "hermes-agent", "status", "--porcelain", "--untracked-files=all"): "",
            ("config", "-f", ".gitmodules", "--get", "submodule.hermes-agent.url"): URL,
        }

    def run_check(self):
        with patch.object(CHECK, "ROOT", self.root), patch.object(CHECK, "git", side_effect=lambda *a: self.responses[a]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return CHECK.main()

    def test_consistent_metadata_passes(self):
        self.assertEqual(self.run_check(), 0)

    def test_revision_and_fork_drift_fail(self):
        path = self.root / "docs/export-provenance.md"
        for content in (f"Current Hermes gitlink: `{'b' * 40}`\nCurrent Hermes fork: `{URL}`\n",
                        f"Current Hermes gitlink: `{REVISION}`\nCurrent Hermes fork: `https://other.invalid/`\n",
                        f"Historical Hermes gitlink: `{REVISION}`\nCurrent Hermes fork: `{URL}`\n"):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(self.run_check(), 1)

    def test_dirty_checkout_and_existing_drift_fail(self):
        for key, value in [
            (("-C", "hermes-agent", "rev-parse", "HEAD"), "b" * 40),
            (("-C", "hermes-agent", "status", "--porcelain", "--untracked-files=all"), "?? file"),
            (("ls-files", "--stage", "--", "hermes-agent"), "conflict"),
        ]:
            with self.subTest(key=key):
                original = self.responses[key]
                self.responses[key] = value
                self.assertEqual(self.run_check(), 1)
                self.responses[key] = original

    def test_compose_and_quickstart_drift_fail(self):
        for filename in ("compose.public.yaml", "docs/public-docker-quickstart.md"):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_text()
                path.write_text(original.replace(REVISION[:8], "b" * 8))
                self.assertEqual(self.run_check(), 1)
                path.write_text(original)

    def test_temporary_root_index_is_not_used_for_submodule_status(self):
        with patch.dict(os.environ, {"GIT_INDEX_FILE": "/tmp/synthetic-index"}), \
                patch.object(CHECK.subprocess, "run") as run:
            run.return_value.stdout = ""
            CHECK.git("ls-files")
            self.assertEqual(run.call_args.kwargs["env"]["GIT_INDEX_FILE"], "/tmp/synthetic-index")
            CHECK.git("-C", "hermes-agent", "status")
            self.assertNotIn("GIT_INDEX_FILE", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
