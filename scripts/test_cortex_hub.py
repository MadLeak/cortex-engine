"""Hub tests. Run: python3 -m unittest scripts/test_cortex_hub.py"""
import json
import subprocess
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

    def test_local_http_links_lan_vaults_over_https(self):
        """--local-http: plain HTTP on this machine, HTTPS on the LAN listener, each with its own port in the links."""
        local = type("LocalHttp", (cortex_hub.Hub,), {"domain": "cortex.localhost", "tls": None, "public_port": 8790,
                                                       "lan_domain": "alastor.cortex.home", "lan_ip": "192.168.1.20",
                                                       "lan_tls": True, "lan_public_port": 443})
        h = local.__new__(local)
        self.assertEqual(h.vault_url("Fern Cortex", "local"), "http://fern.cortex.localhost:8790/")
        self.assertEqual(h.vault_url("Fern Cortex", "lan"), "https://fern.alastor.cortex.home/")
        self.assertEqual(h.url("192.168.1.20", lan=True), "https://192.168.1.20")
        local.lan_public_port = 8443
        self.assertEqual(h.vault_url("Fern Cortex", "lan"), "https://fern.alastor.cortex.home:8443/")

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

    def test_every_view_serves_the_same_shell(self):
        for path in ("/", "/agents", "/wall", "/review", "/connect", "/panel/Fern%20Cortex", "/panel/fern-cortex"):
            with urllib.request.urlopen(self.base + path) as r:
                body = r.read().decode()
                self.assertEqual(r.status, 200, path)
                self.assertTrue(r.headers["Content-Type"].startswith("text/html"), path)
            self.assertIn('id="review"', body, path)
            self.assertIn("data-link", body, path)
            self.assertNotIn("__CSRF__", body)
        self.assertEqual(self.get("/nope")[0], 404)
        self.assertEqual(self.get("/panel")[0], 404)

    def test_review_lists_verdicts_and_applies_only_the_owners_decisions(self):
        status, out = self.post("/api/connect", json.dumps({"agent": "Kale", "kind": "codex", "role": "curator"}).encode())
        self.assertEqual(status, 200, out)
        v = cortex_hub.Hub.root / "Kale Cortex"
        sys.path.insert(0, str(v / "_brain/scripts"))
        import brain_core
        brain = [sys.executable, str(v / "_brain/scripts/brain.py"), "new"]
        subprocess.run(brain + ["--layer", "1", "--title", "Garden", "--summary", "What grows in the beds and when it is watered."], check=True, capture_output=True)
        subprocess.run(brain + ["--layer", "2", "--up", "Garden", "--title", "Kale Waters Weekly",
                                "--summary", "Kale beds get water every Sunday morning, more in a heat wave."], check=True, capture_output=True)
        seed = brain_core.Vault(v).node("Kale Waters Weekly")
        store = cortex_hub.review_dir(cortex_hub.Hub.root, None)
        store.mkdir(parents=True)
        (store / "Kale Cortex.jsonl").write_text(json.dumps({"id": seed.key, "title": seed.title, "action": "consolidate", "hash": "x"}) + "\n")
        self.assertEqual(json.loads(self.get("/api/review")[1])["vaults"][0]["open"], 1)
        self.assertEqual(self.get("/review")[0], 200)
        body = json.dumps({"vault": "Kale Cortex", "decisions": {seed.key: "consolidated"}}).encode()
        self.assertEqual(self.post("/api/review/apply", body, {"X-Cortex-Csrf": "guess"})[0], 403)
        self.assertEqual(self.post("/api/review/apply", json.dumps({"vault": "Kale Cortex", "decisions": {seed.key: "delete"}}).encode())[0], 500)
        status, out = self.post("/api/review/apply", body)
        self.assertEqual(status, 200, out)
        self.assertEqual(json.loads(out)["changed"], 1)
        self.assertEqual(brain_core.Vault(v).by_key()[seed.key].status, "consolidated")
        self.assertEqual(json.loads(self.get("/api/review")[1])["vaults"][0]["open"], 0, "a decided node leaves the queue")


class ScopeTest(unittest.TestCase):
    """A vault marked lan shows on the LAN listener; a local one never leaves this machine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name in ("Fern Cortex", "Kale Cortex"):
            v = root / name / "_brain"
            (v / "scripts").mkdir(parents=True)
            (v / "scripts" / "brain.py").write_text("")
            (v / "Panel.html").write_text(f"<html>{name}</html>")
        data = root / ".cortex-hub"
        data.mkdir()
        cortex_hub.save_scope(data, "Kale Cortex", "lan")
        self.lan = type("LanHub", (cortex_hub.Hub,), {"root": root, "data": data, "scope": "lan", "domain": "cortex.home",
                                                      "allowed": {"cortex.home", "127.0.0.1"}, "lan_domain": "cortex.home"})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.lan)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path, host):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers={"Host": host})) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def test_lan_listener_shows_only_lan_vaults(self):
        self.assertEqual(self.get("/", "kale.cortex.home"), (200, "<html>Kale Cortex</html>"))
        self.assertEqual(self.get("/", "fern.cortex.home")[0], 421, "a local vault has no LAN host")
        (v,) = json.loads(self.get("/api/vaults", "cortex.home")[1])["vaults"]
        self.assertEqual((v["name"], v["scope"]), ("Kale Cortex", "lan"))
        self.assertEqual(self.get("/v/Fern%20Cortex/Panel.html", "cortex.home")[0], 404)
        self.assertEqual(self.get("/v/kale/", "127.0.0.1"), (200, "<html>Kale Cortex</html>"), "by IP, by agent label")
        self.assertEqual(self.get("/v/fern/", "127.0.0.1")[0], 404)

    def test_connect_records_the_scope_and_refuses_lan_without_a_lan_listener(self):
        root, data, lock = self.lan.root, self.lan.data, threading.Lock()
        out = cortex_hub.connect(root, str(root), {"agent": "Fern", "kind": "codex", "vault": "Fern Cortex", "scope": "lan"},
                                 "o", lock, data, lan=True)
        self.assertEqual(out["scope"], "lan")
        self.assertEqual(cortex_hub.load_scopes(data)["Fern Cortex"], "lan")
        with self.assertRaises(ValueError):
            cortex_hub.connect(root, str(root), {"agent": "Fern", "kind": "codex", "vault": "Fern Cortex", "scope": "lan"},
                               "o", lock, data, lan=False)


class TlsTest(unittest.TestCase):
    """A new machine name or address renews the server certificate under the same CA; nothing public can be signed."""

    def test_the_ca_outlives_a_new_address(self):
        import shutil
        import subprocess
        import hub_tls
        if not shutil.which("openssl"):
            self.skipTest("no openssl")
        with tempfile.TemporaryDirectory() as d:
            tls, zones = Path(d), ["cortex.local", "cortex.home", "localhost"]
            hub_tls.ensure(tls, ["cortex.local", "*.cortex.local", "alastor.cortex.home"], ["127.0.0.1", "10.0.0.100"], zones)
            ca = (tls / "ca.crt").read_bytes()
            done = hub_tls.ensure(tls, ["cortex.local", "*.cortex.local", "zeus.cortex.home"], ["127.0.0.1", "192.168.1.9"], zones)[2]
            self.assertEqual((tls / "ca.crt").read_bytes(), ca, "same CA")
            self.assertEqual(len(done), 1, "only the server certificate")
            ok = subprocess.run(["openssl", "verify", "-CAfile", str(tls / "ca.crt"), str(tls / "hub.crt")], capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            # a certificate for a public name, signed with the CA's own key, does not verify
            subprocess.run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                            "-keyout", f"{d}/x.key", "-out", f"{d}/x.csr", "-subj", "/CN=bank.example"], check=True, capture_output=True)
            Path(d, "x.ext").write_text("subjectAltName=DNS:bank.example\n")
            subprocess.run(["openssl", "x509", "-req", "-in", f"{d}/x.csr", "-CA", str(tls / "ca.crt"), "-CAkey", str(tls / "ca.key"),
                            "-set_serial", "1", "-days", "1", "-extfile", f"{d}/x.ext", "-out", f"{d}/x.crt"], check=True, capture_output=True)
            bad = subprocess.run(["openssl", "verify", "-CAfile", str(tls / "ca.crt"), f"{d}/x.crt"], capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("permitted subtree violation", bad.stdout + bad.stderr)


class DnsTest(unittest.TestCase):
    def test_answers_its_domain_only(self):
        import hub_dns
        import struct

        def ask(name, qtype):
            q = b"".join(bytes([len(x)]) + x.encode() for x in name.split(".")) + b"\0"
            r = hub_dns.answer(struct.pack("!HHHHHH", 9, 0x0100, 1, 0, 0, 0) + q + struct.pack("!HH", qtype, 1),
                               {"cortex.local": "127.0.0.1", "alastor.cortex.home": "10.0.0.100"})
            return r[3] & 15, struct.unpack("!H", r[6:8])[0], r[-4:]
        self.assertEqual(ask("claude.cortex.local", 1), (0, 1, bytes([127, 0, 0, 1])))
        self.assertEqual(ask("Cortex.Local", 1)[:2], (0, 1))
        self.assertEqual(ask("claude.cortex.local", 28)[:2], (0, 0), "no IPv6 record, but the name exists")
        self.assertEqual(ask("evilcortex.local", 1)[:2], (5, 0), "refused")
        self.assertEqual(ask("example.com", 1)[:2], (5, 0))
        self.assertEqual(ask("claude.alastor.cortex.home", 1), (0, 1, bytes([10, 0, 0, 100])))
        self.assertEqual(ask("claude.other.cortex.home", 1)[:2], (5, 0), "another machine's hub answers for itself")


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
