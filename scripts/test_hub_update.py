"""Every cortex kept on the latest Cortex when an agent starts a task. Run: python3 -m unittest scripts/test_hub_update.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_update  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "template-vault"
OWNER = {"GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "o@example.com",
         "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "o@example.com"}


def git(v, *a):
    return subprocess.run(["git", "-C", str(v), *a], capture_output=True, text=True, env={**os.environ, **OWNER}).stdout.strip()


class BringCurrentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.v = Path(self.tmp.name) / "Old Cortex"
        shutil.copytree(TEMPLATE / "_brain", self.v / "_brain", ignore=shutil.ignore_patterns("__pycache__"))
        self.core = self.v / "_brain/scripts/brain_core.py"
        self.want = hub_update.hub_version()
        git(self.v, "init", "-q")
        git(self.v, "add", "-A")
        git(self.v, "commit", "-q", "-m", "seed")

    def tearDown(self):
        self.tmp.cleanup()

    def age(self, version="2.0.0", schema=None):
        self.core.write_text(self.core.read_text(encoding="utf-8").replace(f'VERSION = "{self.want}"', f'VERSION = "{version}"'),
                             encoding="utf-8")
        cfg = self.v / "_brain/config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        if schema is None:
            c.pop("schema", None)
        else:
            c["schema"] = schema
        cfg.write_text(json.dumps(c), encoding="utf-8")
        git(self.v, "add", "-A")
        git(self.v, "commit", "-q", "-m", "fossil")

    def test_a_current_vault_is_left_alone(self):
        self.assertEqual(hub_update.bring_current(self.v), [])
        self.assertEqual(git(self.v, "log", "--format=%s"), "seed")

    def test_an_older_engine_and_schema_are_brought_up_and_committed_by_the_hub(self):
        self.age()
        lines = hub_update.bring_current(self.v)
        self.assertEqual(lines[0], f"Old Cortex: engine v2.0.0 -> v{self.want}")
        self.assertTrue(any("applied 0001" in x for x in lines), lines)
        self.assertIn(f'VERSION = "{self.want}"', self.core.read_text(encoding="utf-8"))
        log = git(self.v, "log", "--format=%an|%s|%b")
        self.assertIn(f"Cortex hub|engine: v{self.want}|upgraded by the Cortex hub when an agent started a task", log)
        self.assertIn("Cortex hub|migration: 0003", log)
        self.assertEqual(hub_update.vault_schema(self.v), hub_update.current_schema())
        self.assertEqual(hub_update.bring_current(self.v), [], "current now: nothing to do")

    def test_a_newer_engine_is_left_alone_and_not_reported(self):
        self.age("99.0.0", schema=hub_update.current_schema())
        self.assertEqual(hub_update.bring_current(self.v), [])
        self.assertIn('VERSION = "99.0.0"', self.core.read_text(encoding="utf-8"))

    def test_a_folder_with_no_readable_engine_is_not_touched(self):
        self.core.unlink()
        self.assertEqual(hub_update.bring_current(self.v), [])
        self.assertFalse(self.core.exists())


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".cortex-hub").mkdir()
        self.up = hub_update.Upstream()
        self.have = hub_update.hub_version()

    def tearDown(self):
        self.tmp.cleanup()

    def newer(self):
        x = hub_update.version_tuple(self.have)
        return f"{x[0]}.{x[1] + 1}.0"

    def wait(self):
        for _ in range(100):
            if not self.up.running:
                return
            time.sleep(0.02)

    def test_a_newer_version_upstream_is_said_with_the_commands(self):
        with mock.patch.object(hub_update.Upstream, "fetch", return_value=self.newer()), \
             mock.patch.dict(os.environ, {"CORTEX_UPDATE_CHECK": "1"}):
            self.up.poke(self.root)
            self.wait()
        note = self.up.notice()
        self.assertIn(f"Cortex v{self.newer()} is published; this hub runs v{self.have}", note)
        self.assertIn("git pull", note)
        self.assertIn("docker compose up -d --build", note)
        saved = json.loads((self.root / ".cortex-hub/update-state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["latest"], self.newer())

    def test_the_same_version_says_nothing(self):
        with mock.patch.object(hub_update.Upstream, "fetch", return_value=self.have), \
             mock.patch.dict(os.environ, {"CORTEX_UPDATE_CHECK": "1"}):
            self.up.poke(self.root)
            self.wait()
        self.assertEqual(self.up.notice(), "")

    def test_it_asks_once_per_interval_and_remembers_across_restarts(self):
        fetch = mock.Mock(return_value=self.newer())
        with mock.patch.object(hub_update.Upstream, "fetch", fetch), mock.patch.dict(os.environ, {"CORTEX_UPDATE_CHECK": "1"}):
            self.up.poke(self.root)
            self.wait()
            self.up.poke(self.root)
            self.wait()
            again = hub_update.Upstream()   # a restarted hub reads the last check
            again.poke(self.root)
            self.wait()
        self.assertEqual(fetch.call_count, 1)
        self.assertIn(self.newer(), again.notice())

    def test_offline_is_no_error_and_keeps_the_last_answer(self):
        self.up.latest = self.newer()
        with mock.patch.object(hub_update.Upstream, "fetch", side_effect=OSError("offline")), \
             mock.patch.dict(os.environ, {"CORTEX_UPDATE_CHECK": "1"}):
            self.up.poke(self.root)
            self.wait()
        self.assertIn("offline", self.up.error)
        self.assertIn(self.newer(), self.up.notice())

    def test_turned_off_never_asks(self):
        fetch = mock.Mock(return_value=self.newer())
        with mock.patch.object(hub_update.Upstream, "fetch", fetch), mock.patch.dict(os.environ, {"CORTEX_UPDATE_CHECK": "0"}):
            self.up.poke(self.root)
        self.assertEqual(fetch.call_count, 0)
        self.assertFalse(self.up.running)

    def test_the_address_reads_only_the_head_of_the_engine_on_the_default_branch(self):
        with mock.patch.dict(os.environ, {"CORTEX_UPDATE_REPO": "", "CORTEX_UPDATE_BRANCH": ""}):
            os.environ.pop("CORTEX_UPDATE_REPO")
            self.assertEqual(hub_update.Upstream.url(), "https://raw.githubusercontent.com/MadLeak/cortex-engine/main/"
                                                        "template-vault/_brain/scripts/brain_core.py")


class OnBriefTests(unittest.TestCase):
    def test_an_error_becomes_one_line_never_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(hub_update, "bring_current", side_effect=RuntimeError("disk full")), \
             mock.patch.dict(os.environ, {"CORTEX_UPDATE_CHECK": "0"}):
            out = hub_update.on_brief(Path(tmp) / "X Cortex")
        self.assertEqual(out, "X Cortex: could not be brought up to date: disk full")


if __name__ == "__main__":
    unittest.main()
