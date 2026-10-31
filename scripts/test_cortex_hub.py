"""Hub tests. Run: python3 -m unittest scripts/test_cortex_hub.py"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cortex_hub  # noqa: E402


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        v = root / "Fern Cortex" / "_brain"
        (v / "scripts").mkdir(parents=True)
        (v / "scripts" / "brain.py").write_text("")
        (v / "generated").mkdir()
        (v / "state").mkdir()
        (v / "Panel.html").write_text("<html>panel</html>")
        (v / "config.json").write_text("{}")
        (v / "generated" / "graph.json").write_text(json.dumps({"version": "2.11.0", "cold": {"_inbox": 2}, "nodes": [
            {"layer": 1, "status": "consolidated"}, {"layer": 3, "status": "seed"}, {"layer": 3, "status": "provisional"}]}))
        (v / "state" / "activity.js").write_text(cortex_hub.ACTIVITY_PREFIX + json.dumps({"vault": "Fern Cortex", "events": [
            {"t": "2020-01-01T10:00:00", "e": "recall", "q": "when to water", "i": 1},
            {"t": "2020-01-01T10:00:01", "e": "activated", "title": "Beds", "i": 2}]}) + ";\n")
        (root / "notes").mkdir()   # not a vault: no _brain/scripts/brain.py
        cortex_hub.Hub.root = root
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cortex_hub.Hub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {})) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def post(self, path, data: bytes, headers=None):
        h = {"Origin": self.base, "Content-Type": "application/json", "X-Cortex-Csrf": cortex_hub.Hub.csrf, **(headers or {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=h)) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def test_serves_only_the_panel_files(self):
        self.assertEqual(self.get("/v/Fern%20Cortex/Panel.html"), (200, "<html>panel</html>"))
        self.assertEqual(self.get("/v/Fern%20Cortex/state/activity.js")[0], 200)
        for path in ("/v/Fern%20Cortex/config.json", "/v/Fern%20Cortex/../config.json", "/v/Fern%20Cortex/%2e%2e/config.json",
                     "/v/notes/Panel.html", "/etc/passwd"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_cards_sum_layers_seeds_and_latest_activity(self):
        status, body = self.get("/api/vaults")
        (v,) = json.loads(body)["vaults"]
        self.assertEqual((status, v["name"], v["version"], v["nodes"], v["inbox"]), (200, "Fern Cortex", "2.11.0", 3, 2))
        self.assertEqual((v["layers"], v["status"]["seed"], v["status"]["provisional"]), ({"1": 1, "2": 0, "3": 2}, 1, 1))
        self.assertEqual([e["e"] for e in v["recent"]], ["recall"], "activated events are left out of the card")
        self.assertFalse(v["live"])
        self.assertIn("Cortex hub", self.get("/")[1])

    def test_other_host_names_are_refused(self):
        self.assertEqual(self.get("/api/vaults", {"Host": "evil.example:80"})[0], 421, "DNS rebinding")
        self.assertEqual(self.get("/api/vaults", {"Host": "localhost:8790"})[0], 200)

    def test_each_agent_answers_at_its_own_host(self):
        cortex_hub.Hub.domain = "cortex.localhost"
        try:
            self.assertEqual(self.get("/", {"Host": "fern.cortex.localhost"}), (200, "<html>panel</html>"))
            self.assertEqual(self.get("/state/activity.js", {"Host": "fern.cortex.localhost"})[0], 200)
            self.assertEqual(self.get("/config.json", {"Host": "fern.cortex.localhost"})[0], 404)
            self.assertEqual(self.get("/", {"Host": "kale.cortex.localhost"})[0], 421, "no such agent")
            (v,) = json.loads(self.get("/api/vaults")[1])["vaults"]
            self.assertEqual(v["url"], "http://fern.cortex.localhost:443/")
        finally:
            cortex_hub.Hub.domain = None
        self.assertEqual(cortex_hub.subdomain("OpenCode Cortex"), "opencode")

    def test_connect_needs_same_origin_and_the_page_token(self):
        body = json.dumps({"agent": "Fern", "kind": "codex", "vault": "Fern Cortex", "role": "reader"}).encode()
        self.assertEqual(self.post("/api/connect", body, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/connect", body, {"X-Cortex-Csrf": "guess"})[0], 403)
        status, out = self.post("/api/connect", json.dumps({"agent": "../x", "kind": "codex"}).encode())
        self.assertEqual(status, 400, out)

    def test_joining_a_vault_gives_a_prompt_with_the_hosts_paths(self):
        cortex_hub.Hub.host_root = "C:\\Users\\me/Vaults"   # compose writes ${USERPROFILE}/Vaults
        try:
            status, out = self.post("/api/connect", json.dumps(
                {"agent": "Fern", "kind": "hermes", "vault": "Fern Cortex", "role": "reader", "python": "py"}).encode())
        finally:
            cortex_hub.Hub.host_root = None
        self.assertEqual(status, 200, out)
        out = json.loads(out)
        self.assertFalse(out["created"])
        p = out["prompt"]
        self.assertIn('py "C:\\Users\\me\\Vaults\\Fern Cortex\\_brain\\scripts\\brain.py" doctor', p)
        self.assertIn("<!-- cortex:fern-cortex -->", p)
        self.assertIn("===== SKILL B =====", p, "Hermes gets its skill")
        self.assertIn("--by fern", p)
        self.assertIn("first commit", p, "the fake vault has no git repository")

    def test_creating_a_vault_runs_new_vault(self):
        status, out = self.post("/api/connect", json.dumps({"agent": "Kale", "kind": "codex", "role": "curator"}).encode())
        self.assertEqual(status, 200, out)
        out = json.loads(out)
        v = cortex_hub.Hub.root / "Kale Cortex"
        self.assertTrue(out["created"])
        self.assertTrue((v / "_brain/scripts/brain.py").is_file() and (v / "_brain/Panel.html").is_file())
        self.assertIn("~/.codex/AGENTS.md", out["prompt"])
        self.assertIn("Curate (you are this vault's curator)", out["prompt"])
        self.assertEqual(self.post("/api/connect", json.dumps({"agent": "Kale", "kind": "codex"}).encode())[0], 400,
                         "an existing folder is never overwritten")


class LanTest(unittest.TestCase):
    """The LAN scope: everything but the sign-in page needs the access key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cortex_hub.Hub.root, cortex_hub.Hub.key = Path(self.tmp.name), "test-key-123"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cortex_hub.Hub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        cortex_hub.Hub.key = None
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_sign_in(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedirect)

        def call(path, data=None, cookie=None):
            h = {"Origin": self.base, **({"Cookie": cookie} if cookie else {})}
            try:
                r = op.open(urllib.request.Request(self.base + path, data=data, headers=h))
                return r.status, r.headers
            except urllib.error.HTTPError as e:
                e.close()
                return e.code, e.headers

        self.assertEqual(call("/api/vaults")[0], 401)
        self.assertEqual(call("/")[0], 302)
        self.assertEqual(call("/login")[0], 200)
        self.assertEqual(call("/login", b"key=wrong")[0], 401)
        status, h = call("/login", b"key=test-key-123")
        self.assertEqual(status, 302)
        cookie = h["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(call("/api/vaults", cookie=cookie.split(";")[0])[0], 200)


if __name__ == "__main__":
    unittest.main()
