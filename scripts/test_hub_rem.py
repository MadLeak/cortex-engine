"""REM dreaming inside the hub. Run: python3 -m unittest scripts/test_hub_rem.py"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cortex_hub  # noqa: E402
import hub_rem  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "template-vault"
OWNER = {"CORTEX_GIT_NAME": "Real Owner", "CORTEX_GIT_EMAIL": "owner@example.com"}


class FakeStrata(BaseHTTPRequestHandler):
    """A model server that answers /health and writes five English and five Spanish questions for every note asked."""
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        FakeStrata.calls += 1
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = req["messages"][0]["content"]
        lines = "\n".join(json.dumps({"id": i, "en": [f"how does {i} work {n}" for n in range(5)],
                                      "es": [f"como funciona {i} {n}" for n in range(5)]})
                          for i in re.findall(r"### ID: (\S+)", text))
        body = json.dumps({"choices": [{"message": {"content": lines}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def engine(v, *args):
    r = subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "--vault", str(v), *args],
                       capture_output=True, text=True, cwd=str(v.parent))
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout


class RemBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.v = self.root / "Demo Cortex"
        shutil.copytree(TEMPLATE / "_brain", self.v / "_brain", ignore=shutil.ignore_patterns("__pycache__", "state"))
        shutil.copytree(TEMPLATE / "_templates", self.v / "_templates")
        shutil.copy(TEMPLATE / ".gitignore", self.v / ".gitignore")
        for c in ("_inbox", "_sources", "_log", "_archive"):
            (self.v / c).mkdir(exist_ok=True)
        engine(self.v, "new", "--layer", "1", "--title", "Garden", "--summary", "The garden beds and their care.")
        engine(self.v, "new", "--layer", "2", "--up", "Garden", "--title", "Water before 9 am",
               "--summary", "Beds are watered in the morning only, so leaves dry before night.")
        engine(self.v, "new", "--layer", "2", "--up", "Garden", "--title", "Slugs are the main pest",
               "--summary", "Slugs eat the lettuce; copper tape keeps them off the beds.")
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Setup", "GIT_AUTHOR_EMAIL": "setup@example.com",
                    "GIT_COMMITTER_NAME": "Setup", "GIT_COMMITTER_EMAIL": "setup@example.com"}
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeStrata)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        FakeStrata.calls = 0
        self.owner = mock.patch.dict(os.environ, OWNER)
        self.owner.start()

    def tearDown(self):
        self.owner.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.v), *a], capture_output=True, text=True, env=self.env)


class DreamTest(RemBase):
    def test_a_run_stores_the_questions_and_commits_them_as_the_owner(self):
        res = hub_rem.dream(self.v, 5, strata=self.url)
        self.assertEqual(res["done"], 3, res)          # the domain and the two concepts
        self.assertEqual(res["left"], 0)
        stored = [json.loads(x) for x in (self.v / "_brain" / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(stored), 3)
        self.assertTrue(all(len(d["en"]) == 5 and len(d["es"]) == 5 and d["hash"] for d in stored))
        self.assertEqual(self.git("log", "-1", "--format=%an|%ae|%cn|%ce|%s").stdout.strip(),
                         "Real Owner|owner@example.com|Real Owner|owner@example.com|rem: questions for 3 notes")
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "", "nothing left uncommitted")
        feed = (self.v / "_brain/state/activity.js").read_text(encoding="utf-8")
        self.assertIn('"e":"dreamed"', feed, "the panel's feed carries the dream")
        self.assertIn("REM 3 notes", feed)

    def test_a_second_run_dreams_only_what_changed(self):
        hub_rem.dream(self.v, 5, strata=self.url)
        calls = FakeStrata.calls
        again = hub_rem.dream(self.v, 5, strata=self.url)
        self.assertEqual((again["done"], again["left"], FakeStrata.calls), (0, 0, calls))
        note = self.v / "Garden" / "Water before 9 am.md"
        note.write_text(note.read_text(encoding="utf-8").replace("Beds are watered", "Beds are only watered"), encoding="utf-8")
        third = hub_rem.dream(self.v, 5, strata=self.url)
        self.assertEqual(third["done"], 1, third)

    def test_the_budget_stops_a_run_and_leaves_the_rest(self):
        with mock.patch.object(hub_rem, "BATCH", 1):
            res = hub_rem.dream(self.v, 0, strata=self.url)   # no time at all
        self.assertEqual((res["done"], res["left"]), (0, 3))
        self.assertEqual(FakeStrata.calls, 0)

    def test_it_never_commits_as_anyone_but_the_owner(self):
        with mock.patch.dict(os.environ, {"CORTEX_GIT_NAME": "", "CORTEX_GIT_EMAIL": ""}):
            res = hub_rem.dream(self.v, 5, strata=self.url)
        self.assertIn("CORTEX_GIT_NAME", res["result"])
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1", "no commit, no run")
        self.assertEqual(FakeStrata.calls, 0, "no identity, no model call")

    def test_something_else_staged_is_left_alone(self):
        (self.v / "note.txt").write_text("x", encoding="utf-8")
        self.git("add", "note.txt")
        res = hub_rem.dream(self.v, 5, strata=self.url)
        self.assertIn("something else is staged", res["result"])
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout.split(), ["note.txt"])

    def test_a_silent_model_server_skips_the_run(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        res = hub_rem.dream(self.v, 5, strata=self.url)
        self.assertIn("does not answer", res["result"])
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")


class SchedulerTest(RemBase):
    def sched(self, runs):
        def runner(v, minutes):
            runs.append((v.name, minutes))
            return {"t": hub_rem.now(), "done": 1, "left": 0, "result": "dreamed 1 notes, 0 left"}
        return hub_rem.Scheduler(self.root, lambda: {self.v.name: self.v}, runner)

    def test_off_by_default_and_on_when_asked(self):
        runs = []
        s = self.sched(runs)
        s.tick()
        self.assertEqual(runs, [], "REM is opt-in")
        cortex_hub.hub_settings.save(self.v, {"rem_enabled": True, "rem_interval_minutes": 60, "rem_budget_minutes": 7})
        s.tick()
        self.assertEqual(runs, [("Demo Cortex", 7.0)])
        s.tick()
        self.assertEqual(len(runs), 1, "the interval is not up yet")
        state = hub_rem.read_state(self.root)
        state["Demo Cortex"]["t"] = "2020-01-01T00:00:00+00:00"
        hub_rem.write_state(self.root, state)
        s.tick()
        self.assertEqual(len(runs), 2, "the interval is up")

    def test_settings_validate_the_rem_keys(self):
        hs = cortex_hub.hub_settings
        for bad in ({"rem_enabled": "maybe"}, {"rem_interval_minutes": 5}, {"rem_budget_minutes": 1000}):
            with self.assertRaises(ValueError, msg=str(bad)):
                hs.save(self.v, bad)
        hs.save(self.v, {"rem_enabled": "true"})
        self.assertIs(json.loads((self.v / "_brain/config.json").read_text(encoding="utf-8"))["rem_enabled"], True)
        self.assertIs(hs.read(self.v, "local")["config"]["rem_enabled"], True)
        self.assertEqual(self.git("log", "-1", "--format=%an").stdout.strip(), "Real Owner")

    def test_a_cortex_dreams_one_run_at_a_time(self):
        gate, runs = threading.Event(), []

        def runner(v, minutes):
            runs.append(1)
            gate.wait(5)
            return {"t": hub_rem.now(), "done": 0, "left": 0, "result": "x"}
        s = hub_rem.Scheduler(self.root, lambda: {self.v.name: self.v}, runner)
        self.assertTrue(s.start_now(self.v))
        self.assertTrue(s.status(self.v)["running"])
        self.assertFalse(s.start_now(self.v), "already dreaming")
        gate.set()
        for _ in range(100):
            if not s.status(self.v)["running"]:
                break
            threading.Event().wait(0.05)
        self.assertEqual(len(runs), 1)
        self.assertEqual(s.status(self.v)["result"], "x")

    def test_a_failing_run_is_a_status_line_not_a_crash(self):
        def runner(v, minutes):
            raise RuntimeError("boom")
        s = hub_rem.Scheduler(self.root, lambda: {self.v.name: self.v}, runner)
        self.assertIn("boom", s.run(self.v)["result"])


class HubRoutesTest(RemBase):
    def setUp(self):
        super().setUp()
        cortex_hub.Hub.root = self.root
        cortex_hub.Hub._rem = None
        self.hub = ThreadingHTTPServer(("127.0.0.1", 0), cortex_hub.Hub)
        threading.Thread(target=self.hub.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.hub.server_address[1]}"

    def tearDown(self):
        self.hub.shutdown()
        self.hub.server_close()
        cortex_hub.Hub._rem = None
        super().tearDown()

    def call(self, path, data=None):
        h = {"Origin": self.base, "Content-Type": "application/json", "X-Cortex-Csrf": cortex_hub.Hub.csrf}
        req = urllib.request.Request(self.base + path, data=data, headers=h if data is not None else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_settings_show_rem_and_dream_now_runs_in_the_background(self):
        code, out = self.call("/api/cortex/Demo%20Cortex/settings")
        self.assertEqual(code, 200)
        self.assertEqual((out["config"]["rem_enabled"], out["config"]["rem_interval_minutes"]), (False, 1440))
        self.assertFalse(out["rem"]["enabled"])
        code, out = self.call("/api/cortex/Demo%20Cortex/settings", json.dumps({"config": {"rem_enabled": "true"}}).encode())
        self.assertEqual(code, 200, out)
        sched = cortex_hub.Hub.rem_scheduler()
        with mock.patch.object(hub_rem, "dream", lambda v, minutes, **kw: {"t": hub_rem.now(), "done": 2, "left": 0,
                                                                         "result": "dreamed 2 notes, 0 left"}):
            sched.runner = hub_rem.dream
            code, out = self.call("/api/cortex/Demo%20Cortex/rem-run", b"{}")
            self.assertEqual(code, 200, out)
            for _ in range(100):
                if not sched.status(self.v)["running"] and sched.status(self.v)["result"]:
                    break
                threading.Event().wait(0.05)
        code, out = self.call("/api/cortex/Demo%20Cortex/settings")
        self.assertEqual((out["rem"]["enabled"], out["rem"]["result"]), (True, "dreamed 2 notes, 0 left"))
        self.assertEqual(self.call("/api/cortex/Nope/rem-run", b"{}")[0], 404)


if __name__ == "__main__":
    unittest.main()
