"""A cortex kept in step with its remote. Run: python3 -m unittest scripts/test_hub_sync.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_settings  # noqa: E402
import hub_sync  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "template-vault"
OWNER = {"CORTEX_GIT_NAME": "Real Owner", "CORTEX_GIT_EMAIL": "owner@example.com"}
SETUP = {"GIT_AUTHOR_NAME": "Setup", "GIT_AUTHOR_EMAIL": "setup@example.com",
         "GIT_COMMITTER_NAME": "Setup", "GIT_COMMITTER_EMAIL": "setup@example.com"}


def git(path, *a):
    r = subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, env={**os.environ, **SETUP})
    return r.stdout.strip()


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.v = self.root / "Demo Cortex"
        (self.v / "_brain").mkdir(parents=True)
        shutil.copy(TEMPLATE / ".gitignore", self.v / ".gitignore")
        (self.v / "Garden.md").write_text("garden\n", encoding="utf-8")
        (self.v / "Pests.md").write_text("pests\n", encoding="utf-8")
        self.cfg({"vault": "Demo Cortex", "sync_enabled": True})
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        git(self.v, "init", "-q", "-b", "main")
        git(self.v, "add", "-A")
        git(self.v, "commit", "-q", "-m", "seed")
        git(self.v, "remote", "add", "origin", str(self.remote))
        git(self.v, "push", "-q", "-u", "origin", "main")
        self.other = self.root / "other"   # another machine
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.other)], check=True)
        self.patches = [mock.patch.dict(os.environ, OWNER), mock.patch.object(hub_sync, "REQUIRE_HTTPS", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def cfg(self, extra):
        p = self.v / "_brain" / "config.json"
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        d.update(extra)
        p.write_text(json.dumps(d), encoding="utf-8")

    def elsewhere(self, name, text, message="elsewhere"):
        (self.other / name).write_text(text, encoding="utf-8")
        git(self.other, "add", "-A")
        git(self.other, "commit", "-q", "-m", message)
        git(self.other, "push", "-q", "origin", "main")


class CycleTests(SyncBase):
    def test_commits_what_changed_as_the_owner_and_pushes(self):
        (self.v / "Garden.md").write_text("garden, edited\n", encoding="utf-8")
        (self.v / "New.md").write_text("new note\n", encoding="utf-8")
        res = hub_sync.cycle(self.v)
        self.assertEqual(res["result"], "ok")
        self.assertTrue(res["committed"])
        self.assertEqual(res["pushed"], 1)
        self.assertEqual(git(self.v, "status", "--porcelain"), "")
        self.assertEqual(git(self.v, "log", "-1", "--format=%an <%ae>"), "Real Owner <owner@example.com>")
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s"), git(self.v, "log", "-1", "--format=%s"))

    def test_a_clean_and_current_cortex_does_nothing(self):
        res = hub_sync.cycle(self.v)
        self.assertEqual((res["result"], res["committed"], res["pulled"], res["pushed"]), ("ok", False, 0, 0))

    def test_pulls_a_remote_that_moved(self):
        self.elsewhere("Garden.md", "garden from the other machine\n")
        res = hub_sync.cycle(self.v)
        self.assertEqual((res["result"], res["pulled"]), ("ok", 1))
        self.assertEqual((self.v / "Garden.md").read_text(encoding="utf-8"), "garden from the other machine\n")

    def test_rebases_when_both_changed_different_notes_and_pushes(self):
        self.elsewhere("Garden.md", "garden from the other machine\n")
        (self.v / "Pests.md").write_text("pests, edited here\n", encoding="utf-8")
        res = hub_sync.cycle(self.v)
        self.assertEqual(res["result"], "ok")
        self.assertEqual((res["pulled"], res["pushed"]), (1, 1))
        self.assertEqual(git(self.v, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "main"))
        self.assertEqual(git(self.v, "status", "--porcelain"), "")

    def test_a_conflict_is_aborted_left_clean_and_reported(self):
        self.elsewhere("Garden.md", "their garden\n")
        (self.v / "Garden.md").write_text("my garden\n", encoding="utf-8")
        res = hub_sync.cycle(self.v)
        self.assertTrue(res["result"].startswith("diverged"), res["result"])
        self.assertFalse(hub_sync.busy(self.v))   # no rebase left half done
        self.assertEqual(git(self.v, "status", "--porcelain"), "")
        self.assertEqual((self.v / "Garden.md").read_text(encoding="utf-8"), "my garden\n")   # my change is kept, committed
        self.assertNotEqual(git(self.remote, "rev-parse", "main"), git(self.v, "rev-parse", "HEAD"))   # nothing forced


class SafetyTests(SyncBase):
    def test_without_the_owners_identity_nothing_is_committed(self):
        (self.v / "Garden.md").write_text("edited\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CORTEX_GIT_NAME": "", "CORTEX_GIT_EMAIL": ""}):
            res = hub_sync.cycle(self.v)
        self.assertIn("CORTEX_GIT_NAME", res["result"])
        self.assertEqual(git(self.v, "log", "--oneline").count("\n"), 0)   # still only the seed
        self.assertEqual(git(self.v, "diff", "--cached", "--name-only"), "")

    def test_no_remote_is_said_not_guessed(self):
        git(self.v, "remote", "remove", "origin")
        self.assertIn("no remote", hub_sync.cycle(self.v)["result"])

    def test_https_and_a_token_are_required_in_production(self):
        with mock.patch.object(hub_sync, "REQUIRE_HTTPS", True):
            self.assertIn("https only", hub_sync.cycle(self.v)["result"])   # the test remote is a local path
            git(self.v, "remote", "set-url", "origin", "https://example.invalid/x/y.git")
            self.assertIn("needs a push token", hub_sync.cycle(self.v)["result"])

    def test_a_git_operation_in_flight_is_left_alone(self):
        (self.v / ".git" / "MERGE_HEAD").write_text("x", encoding="utf-8")
        (self.v / "Garden.md").write_text("edited\n", encoding="utf-8")
        self.assertIn("in flight", hub_sync.cycle(self.v)["result"])
        self.assertIn("M", git(self.v, "status", "--porcelain"))   # untouched

    def test_an_agents_folder_inside_the_vault_is_never_committed(self):
        (self.v / ".claude").mkdir()
        (self.v / ".claude" / "launch.json").write_text("{}", encoding="utf-8")
        (self.v / "Garden.md").write_text("edited\n", encoding="utf-8")
        hub_sync.cycle(self.v)
        self.assertNotIn(".claude", git(self.v, "ls-files"))

    def test_a_commit_git_refuses_leaves_nothing_staged(self):
        # the vault's pre-commit hook refusing a commit (lint) is the real case; a failing signature is the same to git
        # and needs no executable file, which the container's noexec /tmp would not run
        git(self.v, "config", "commit.gpgsign", "true")
        git(self.v, "config", "gpg.program", "/nonexistent-gpg")
        (self.v / "Garden.md").write_text("edited\n", encoding="utf-8")
        res = hub_sync.cycle(self.v)
        self.assertIn("commit refused", res["result"])
        self.assertEqual(git(self.v, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.remote, "log", "--oneline").count("\n"), 0)

    def test_the_token_never_comes_back_in_a_message(self):
        fake = subprocess.CompletedProcess([], 1, stdout="", stderr="fatal: bad header Authorization: Basic ABC secret-tok")
        with mock.patch.object(hub_settings, "git", return_value=fake):
            _, text = hub_sync.run(self.v, ["fetch"], "secret-tok")
        self.assertNotIn("secret-tok", text)

    def test_the_vaults_own_token_wins_over_the_shared_one(self):
        shared = self.root / ".cortex-hub"
        shared.mkdir()
        (shared / "push.token").write_text("shared-token\n", encoding="utf-8")
        self.assertEqual(hub_sync.token(self.v), "shared-token")
        own = hub_settings.push_token_path(self.v)
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("own-token\n", encoding="utf-8")
        self.assertEqual(hub_sync.token(self.v), "own-token")
        self.assertTrue(hub_settings.read(self.v, "local")["push_token"])


class StaleAndSchedulerTests(SyncBase):
    def test_pull_if_stale_is_silent_when_sync_is_off(self):
        self.cfg({"sync_enabled": False})
        self.elsewhere("Garden.md", "from elsewhere\n")
        self.assertEqual(hub_sync.pull_if_stale(self.v), "")
        self.assertEqual((self.v / "Garden.md").read_text(encoding="utf-8"), "garden\n")

    def test_a_cortex_coming_back_after_a_while_pulls_but_never_pushes(self):
        self.elsewhere("Garden.md", "from elsewhere\n")
        (self.v / "Pests.md").write_text("local edit\n", encoding="utf-8")
        self.assertEqual(hub_sync.pull_if_stale(self.v), "sync: pulled 1")
        self.assertEqual((self.v / "Garden.md").read_text(encoding="utf-8"), "from elsewhere\n")
        self.assertNotEqual(git(self.remote, "rev-parse", "main"), git(self.v, "rev-parse", "HEAD"))   # not pushed
        self.assertEqual(hub_sync.pull_if_stale(self.v), "")   # just synced: not stale any more

    def test_due_waits_for_the_interval_and_retries_a_failure_sooner(self):
        s = hub_sync.Scheduler(self.root, lambda: {self.v.name: self.v})
        self.assertTrue(s.due(self.v, None))
        recent = hub_sync.now()
        self.assertFalse(s.due(self.v, {"t": recent, "result": "ok"}))
        old = "2000-01-01T00:00:00+00:00"
        self.assertTrue(s.due(self.v, {"t": old, "result": "ok"}))
        self.cfg({"sync_interval_minutes": 600})
        self.assertFalse(s.due(self.v, {"t": recent, "result": "ok"}))
        self.assertFalse(s.due(self.v, {"t": recent, "result": "fetch failed: x"}))   # retry only after RETRY_MINUTES
        self.cfg({"sync_enabled": False})
        self.assertFalse(s.due(self.v, None))

    def test_tick_syncs_and_records_the_result(self):
        s = hub_sync.Scheduler(self.root, lambda: {self.v.name: self.v})
        (self.v / "Garden.md").write_text("edited\n", encoding="utf-8")
        s.tick()
        st = hub_sync.read_state(self.root)[self.v.name]
        self.assertEqual((st["result"], st["pushed"]), ("ok", 1))
        self.assertEqual(s.status(self.v)["last"], st["t"])
        self.assertFalse(s.status(self.v)["running"])

    def test_start_now_refuses_a_cortex_already_syncing(self):
        s = hub_sync.Scheduler(self.root, lambda: {self.v.name: self.v})
        s.running.add(self.v.name)
        self.assertFalse(s.start_now(self.v))


if __name__ == "__main__":
    unittest.main()
