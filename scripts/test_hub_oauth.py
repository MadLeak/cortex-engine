# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Agent sessions (OAuth 2.1) and the abuse checks on the hub's MCP service. Run: python3 -m unittest scripts/test_hub_oauth.py"""
import base64
import hashlib
import json
import os
import io
import secrets
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, quote, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cortex_hub  # noqa: E402
import hub_oauth  # noqa: E402

cortex_mcp = cortex_hub.cortex_mcp
PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
REDIRECT = "http://localhost:33418/callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def make_vault(root: Path, name: str) -> Path:
    v = root / name
    (v / "_brain" / "scripts").mkdir(parents=True)
    (v / "_brain" / "scripts" / "brain.py").write_text("")
    (v / "_brain" / "config.json").write_text("{}")
    return v


class Base(unittest.TestCase):
    """A hub on a free localhost port with two cortexes, OAuth on and a clock the test moves."""
    extra: dict = {}
    host: str | None = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fern_v, self.kale_v = make_vault(root, "Fern Cortex"), make_vault(root, "Kale Cortex")
        self.data = root / ".cortex-hub"
        self.data.mkdir()
        cortex_hub.save_scope(self.data, "Kale Cortex", "lan")
        self.fern, self.kale = cortex_mcp.ensure_token(self.fern_v), cortex_mcp.ensure_token(self.kale_v)
        self.now = 1_000_000.0
        clock = lambda: self.now
        self.hook = os.environ.pop("CORTEX_ALERT_WEBHOOK", None)
        hub_oauth.DATA = self.data
        self.store = hub_oauth.Store(self.data / "oauth.json", clock)
        self.cls = type("OAuthHub", (cortex_hub.Hub,), {
            "root": root, "data": self.data, "scope": "localhost", "domain": None, "key": None, "sessions": {},
            "mcp_hub": cortex_mcp.Hub(root, 10.0), "mcp_require_token": True, "mcp_tokens": {},
            "oauth": self.store, "limiter": hub_oauth.Limiter(clock), "trusted_proxies": [],
            "reg_limiter": hub_oauth.Limiter(clock, hub_oauth.REG_LIMIT, hub_oauth.REG_WINDOW, hub_oauth.REG_BLOCK),
            **self.extra})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.cls)
        port = self.httpd.server_address[1]
        self.cls.public_port = port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{port}"
        self.public = self.cls.mcp_base(self.cls.__new__(self.cls))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        hub_oauth.DATA = None
        if self.hook is not None:
            os.environ["CORTEX_ALERT_WEBHOOK"] = self.hook
        self.tmp.cleanup()

    def call(self, path, data=None, headers=None, method=None):
        h = {**({"Host": self.host} if self.host else {}), **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode(), dict(e.headers)

    def ping(self, vault: str, token: str | None, headers=None) -> int:
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        return self.call(f"/mcp/{vault}", PING, {"Content-Type": "application/json", **auth, **(headers or {})})[0]

    def alerts(self) -> list[dict]:
        f = self.data / "alerts.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def register(self, name="Claude Code", uris=(REDIRECT,)) -> str:
        status, body, _ = self.call("/oauth/register", json.dumps({"client_name": name, "redirect_uris": list(uris)}).encode(),
                                    {"Content-Type": "application/json"})
        self.assertEqual(status, 201, body)
        return json.loads(body)["client_id"]

    def params(self, cid, challenge, vault="fern", **over) -> dict:
        p = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": challenge,
             "code_challenge_method": "S256", "resource": f"{self.public}/mcp/{vault}", "state": "st-1"}
        p.update(over)
        return {k: v for k, v in p.items() if v is not None}

    def approve(self, params: dict, decision="approve", headers=None):
        form = urlencode({**params, "csrf": self.cls.csrf, "decision": decision}).encode()
        origin = f"https://{self.host}" if self.host else self.base
        return self.call("/oauth/authorize", form, {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded",
                                                    **(headers or {})})

    def token(self, **form):
        status, body, _ = self.call("/oauth/token", urlencode(form).encode(),
                                    {"Content-Type": "application/x-www-form-urlencoded"})
        return status, json.loads(body)

    def flow(self, vault="fern") -> tuple[str, dict]:
        cid = self.register()
        verifier, challenge = pkce()
        p = self.params(cid, challenge, vault)
        status, page, _ = self.call("/oauth/authorize?" + urlencode(p))
        self.assertEqual(status, 200, page)
        self.assertIn("Claude Code", page)
        status, _, headers = self.approve(p)
        self.assertEqual(status, 302)
        q = parse_qs(urlsplit(headers["Location"]).query)
        self.assertTrue(headers["Location"].startswith(REDIRECT + "?"))
        self.assertEqual(q["state"], ["st-1"])
        status, pair = self.token(grant_type="authorization_code", code=q["code"][0], code_verifier=verifier,
                                  client_id=cid, redirect_uri=REDIRECT, resource=p["resource"])
        self.assertEqual(status, 200, pair)
        self.assertEqual((pair["token_type"], pair["expires_in"]), ("Bearer", 3600))
        return cid, pair


class DiscoveryTest(Base):
    def test_discovery_documents_and_the_401_header(self):
        status, body, _ = self.call("/.well-known/oauth-authorization-server")
        meta = json.loads(body)
        self.assertEqual((status, meta["issuer"]), (200, self.public))
        self.assertEqual(meta["token_endpoint"], f"{self.public}/oauth/token")
        self.assertEqual(meta["registration_endpoint"], f"{self.public}/oauth/register")
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(meta["token_endpoint_auth_methods_supported"], ["none"])
        status, body, _ = self.call("/.well-known/oauth-protected-resource/mcp/fern")
        prm = json.loads(body)
        self.assertEqual((status, prm["resource"], prm["authorization_servers"]),
                         (200, f"{self.public}/mcp/fern", [self.public]))
        self.assertEqual(self.call("/.well-known/oauth-protected-resource")[0], 200)
        self.assertEqual(self.call("/.well-known/oauth-protected-resource/mcp/nope")[0], 404)
        status, _, headers = self.call("/mcp/fern", PING, {"Content-Type": "application/json"})
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"],
                         f'Bearer resource_metadata="{self.public}/.well-known/oauth-protected-resource/mcp/fern"')
        self.assertEqual(self.ping("fern", self.fern), 200, "the static token works as before")

    def test_registration_takes_public_clients_with_loopback_or_https_redirects(self):
        status, body, _ = self.call("/oauth/register", json.dumps({
            "client_name": "Codex", "redirect_uris": [REDIRECT, "https://agent.example/cb"],
            "token_endpoint_auth_method": "client_secret_basic"}).encode(), {"Content-Type": "application/json"})
        got = json.loads(body)
        self.assertEqual((status, got["token_endpoint_auth_method"], got["client_name"]), (201, "none", "Codex"))
        self.assertNotIn("client_secret", got)
        self.assertIsNotNone(self.store.client(got["client_id"]))
        for uris in (["http://evil.example/cb"], ["http://localhost/cb#frag"], [], "x"):
            status, body, _ = self.call("/oauth/register", json.dumps({"redirect_uris": uris}).encode(),
                                        {"Content-Type": "application/json"})
            self.assertEqual((status, json.loads(body)["error"]), (400, "invalid_redirect_uri"), uris)


class FlowTest(Base):
    def test_authorize_refuses_missing_or_plain_pkce_and_a_bad_resource(self):
        cid = self.register()
        _, challenge = pkce()
        for over, error in (({"code_challenge": None, "code_challenge_method": None}, "invalid_request"),
                            ({"code_challenge_method": "plain"}, "invalid_request"),
                            ({"code_challenge_method": None}, "invalid_request"),
                            ({"resource": None}, "invalid_target"),
                            ({"resource": f"{self.public}/mcp"}, "invalid_target"),
                            ({"resource": "https://evil.example/mcp/fern"}, "invalid_target")):
            p = self.params(cid, challenge, **over)
            status, _, headers = self.call("/oauth/authorize?" + urlencode(p))
            self.assertEqual(status, 302, over)
            q = parse_qs(urlsplit(headers["Location"]).query)
            self.assertEqual((q["error"], q["state"]), ([error], ["st-1"]), over)
            self.assertNotIn("code", q)
            status, _, headers = self.approve(p)
            self.assertNotIn("code", parse_qs(urlsplit(headers.get("Location", "")).query), over)
        self.assertEqual(self.call("/oauth/authorize?" + urlencode(self.params("nobody", challenge)))[0], 400)
        self.assertEqual(self.call("/oauth/authorize?" + urlencode(
            self.params(cid, challenge, redirect_uri="https://evil.example/cb")))[0], 400, "never redirect elsewhere")
        p = self.params(cid, challenge, redirect_uri="http://localhost:50111/callback")
        self.assertEqual(self.call("/oauth/authorize?" + urlencode(p))[0], 200, "a loopback redirect on any port")

    def test_the_approval_needs_the_page_token_and_same_origin_and_deny_says_so(self):
        cid = self.register()
        _, challenge = pkce()
        p = self.params(cid, challenge)
        form = urlencode({**p, "csrf": "stale", "decision": "approve"}).encode()
        self.assertEqual(self.call("/oauth/authorize", form, {"Origin": self.base})[0], 403)
        self.assertEqual(self.approve(p, headers={"Origin": "http://evil.example"})[0], 403)
        status, _, headers = self.approve(p, "deny")
        self.assertEqual((status, parse_qs(urlsplit(headers["Location"]).query)["error"]), (302, ["access_denied"]))

    def test_the_code_flow_gives_a_token_for_its_vault_only(self):
        _, pair = self.flow("fern")
        self.assertEqual(self.ping("fern", pair["access_token"]), 200)
        self.assertEqual(self.ping("Fern%20Cortex", pair["access_token"]), 200, "the slug and the name are one vault")
        self.assertEqual(self.ping("kale", pair["access_token"]), 401, "a token of fern is refused at kale")
        self.assertEqual(self.ping("fern", pair["access_token"]), 401, "that abuse revoked the session")
        self.assertEqual(self.token(grant_type="refresh_token", refresh_token=pair["refresh_token"])[1]["error"],
                         "invalid_grant")
        (a,) = self.alerts()
        self.assertEqual((a["kind"], a["vault"]), ("oauth_wrong_vault", "Fern Cortex"))
        stored = (self.data / "oauth.json").read_text(encoding="utf-8") + (self.data / "alerts.jsonl").read_text(encoding="utf-8")
        for secret in (pair["access_token"], pair["refresh_token"]):
            self.assertNotIn(secret, stored, "tokens are kept only as hashes and never alerted")
        self.assertEqual(self.ping("kale", self.kale), 200, "kale's static token is untouched")

    def test_a_code_is_single_use_and_bound_to_its_verifier(self):
        cid = self.register()
        verifier, challenge = pkce()
        p = self.params(cid, challenge)
        code = parse_qs(urlsplit(self.approve(p)[2]["Location"]).query)["code"][0]
        form = dict(grant_type="authorization_code", code=code, client_id=cid, redirect_uri=REDIRECT)
        self.assertEqual(self.token(**form, code_verifier=pkce()[0])[1]["error"], "invalid_grant", "another verifier")
        code = parse_qs(urlsplit(self.approve(p)[2]["Location"]).query)["code"][0]
        form["code"] = code
        status, pair = self.token(**form, code_verifier=verifier)
        self.assertEqual(status, 200)
        self.assertEqual(self.token(**form, code_verifier=verifier)[1]["error"], "invalid_grant", "used once")
        self.assertEqual(self.ping("fern", pair["access_token"]), 401, "a replayed code revokes what it gave")
        code = parse_qs(urlsplit(self.approve(p)[2]["Location"]).query)["code"][0]
        self.now += 61
        self.assertEqual(self.token(**dict(form, code=code), code_verifier=verifier)[1]["error"], "invalid_grant",
                         "60 s life")


class ExpiryTest(Base):
    def test_idle_expiry_slides_with_each_call(self):
        _, pair = self.flow()
        a = pair["access_token"]
        for _ in range(3):
            self.now += 3500
            self.assertEqual(self.ping("fern", a), 200, "each call extends the idle timeout")
        self.now += 3601
        self.assertEqual(self.ping("fern", a), 401, "60 minutes without use")
        status, fresh = self.token(grant_type="refresh_token", refresh_token=pair["refresh_token"])
        self.assertEqual(status, 200, "the session itself lives on: the client refreshes")
        self.assertEqual(self.ping("fern", fresh["access_token"]), 200)

    def test_an_access_token_dies_at_24_hours_however_used(self):
        _, pair = self.flow()
        start = self.now
        while self.now + 3000 - start <= 86400:
            self.now += 3000
            self.assertEqual(self.ping("fern", pair["access_token"]), 200)
        self.now += 3000
        self.assertEqual(self.ping("fern", pair["access_token"]), 401, "the 24 h cap")


class RefreshTest(Base):
    def refresh(self, token, cid=None):
        return self.token(grant_type="refresh_token", refresh_token=token, **({"client_id": cid} if cid else {}))

    def test_rotation_the_retry_grace_and_an_older_token_revoking_the_family(self):
        cid, p1 = self.flow()
        status, p2 = self.refresh(p1["refresh_token"], cid)
        self.assertEqual(status, 200)
        self.assertNotEqual((p2["access_token"], p2["refresh_token"]), (p1["access_token"], p1["refresh_token"]))
        self.assertEqual(self.ping("fern", p2["access_token"]), 200)
        self.assertEqual(self.ping("fern", p1["access_token"]), 401, "only the latest pair works")
        self.now += 10
        self.assertEqual(self.refresh(p1["refresh_token"], cid), (200, p2), "a retry within 30 s: the same pair")
        status, p3 = self.refresh(p2["refresh_token"], cid)
        self.assertEqual(status, 200)
        self.assertEqual(self.alerts(), [])
        self.now += 5
        status, got = self.refresh(p1["refresh_token"], cid)
        self.assertEqual((status, got["error"]), (400, "invalid_grant"), "an older refresh token")
        self.assertEqual(self.ping("fern", p3["access_token"]), 401, "the whole family is revoked")
        self.assertEqual(self.refresh(p3["refresh_token"], cid)[1]["error"], "invalid_grant")
        (a,) = self.alerts()
        self.assertEqual((a["kind"], a["vault"]), ("refresh_reuse", "Fern Cortex"))

    def test_the_previous_refresh_token_after_30_s_revokes_the_family(self):
        cid, p1 = self.flow()
        _, p2 = self.refresh(p1["refresh_token"], cid)
        self.now += 31
        self.assertEqual(self.refresh(p1["refresh_token"], cid)[1]["error"], "invalid_grant")
        self.assertEqual(self.ping("fern", p2["access_token"]), 401)
        self.assertEqual(self.refresh(p2["refresh_token"], cid)[1]["error"], "invalid_grant")
        self.assertEqual([a["kind"] for a in self.alerts()], ["refresh_reuse"])

    def test_a_refresh_token_of_another_client_is_refused(self):
        _, p1 = self.flow()
        other = self.register("Gemini CLI")
        self.assertEqual(self.refresh(p1["refresh_token"], other)[1]["error"], "invalid_grant")
        self.assertEqual(self.refresh("guess")[1]["error"], "invalid_grant")

    def test_sessions_survive_a_restart(self):
        _, p1 = self.flow()
        self.cls.oauth = hub_oauth.Store(self.data / "oauth.json", lambda: self.now)
        self.assertEqual(self.ping("fern", p1["access_token"]), 200)
        self.assertEqual(self.refresh(p1["refresh_token"])[0], 200)


class OwnerTest(Base):
    def test_the_owner_lists_and_revokes_sessions(self):
        _, pair = self.flow()
        status, body, _ = self.call("/api/oauth")
        (s,) = json.loads(body)["sessions"]
        self.assertEqual((status, s["client"], s["vault"], s["state"]), (200, "Claude Code", "Fern Cortex", "active"))
        self.assertNotIn(pair["access_token"], body)
        revoke = f"/api/oauth/{s['id']}/revoke"
        self.assertEqual(self.call(revoke, b"{}", {"Origin": self.base})[0], 403, "the page's token")
        self.assertEqual(self.call(revoke, b"{}", {"Origin": self.base, "X-Cortex-Csrf": self.cls.csrf})[0], 200)
        self.assertEqual(self.ping("fern", pair["access_token"]), 401)
        self.assertEqual(json.loads(self.call("/api/oauth")[1])["sessions"][0]["state"], "revoked")


class LanOwnerTest(Base):
    """Behind a reverse proxy: discovery and the token endpoint are public, the approval page is the owner's."""
    host = "cortex.example.com"
    extra = {"scope": "lan", "domain": "athena.cortex.home", "key": "the-access-key", "lan_domain": "athena.cortex.home",
             "allowed": {"athena.cortex.home", "cortex.example.com"}, "lan_names": ["cortex.example.com"]}

    def test_authorize_is_for_the_signed_in_owner_and_comes_back_after_signing_in(self):
        self.assertEqual(self.public, "https://cortex.example.com")
        self.assertEqual(json.loads(self.call("/.well-known/oauth-authorization-server")[1])["issuer"], self.public)
        cid = self.register()
        _, challenge = pkce()
        p = self.params(cid, challenge, "kale")
        path = "/oauth/authorize?" + urlencode(p)
        status, _, headers = self.call(path)
        self.assertEqual((status, headers["Location"]), (302, "/login?next=" + quote(path, safe="")))
        self.assertEqual(self.approve(p)[0], 401, "not signed in: no approval")
        status, page, _ = self.call("/login?next=" + quote(path, safe=""))
        self.assertIn('name="next"', page)
        status, _, headers = self.call("/login", urlencode({"key": "the-access-key", "next": path}).encode())
        self.assertEqual((status, headers["Location"]), (302, path), "back to the approval")
        cookie = {"Cookie": headers["Set-Cookie"].split(";")[0]}
        _, _, headers = self.call("/login", urlencode({"key": "the-access-key", "next": "https://evil.example/"}).encode())
        self.assertEqual(headers["Location"], "/", "never back to another site")
        self.assertEqual(self.call(path, headers=cookie)[0], 200)
        self.assertEqual(self.call("/oauth/authorize?" + urlencode(self.params(cid, challenge, "fern")), headers=cookie)[2]
                         .get("Location", "").count("invalid_target"), 1, "a cortex not on this listener")
        status, _, headers = self.approve(p, headers=cookie)
        self.assertIn("code=", headers["Location"])

    def test_without_an_access_key_nobody_is_the_owner(self):
        self.cls.key = None
        cid = self.register()
        self.assertEqual(self.call("/oauth/authorize?" + urlencode(self.params(cid, pkce()[1], "kale")))[0], 403)


class StaticTokenTest(Base):
    def test_a_vaults_static_token_at_another_vault_rotates_it(self):
        self.assertEqual(self.ping("kale", self.fern), 401)
        new = cortex_mcp.token_path(self.fern_v).read_text(encoding="utf-8").strip()
        self.assertNotEqual(new, self.fern)
        self.assertGreaterEqual(len(new), 40)
        self.assertEqual(cortex_mcp.token_path(self.kale_v).read_text(encoding="utf-8").strip(), self.kale,
                         "the targeted vault's token is never touched")
        self.assertEqual(self.ping("fern", self.fern), 401, "the leaked token no longer works")
        self.assertEqual(self.ping("fern", new), 200)
        self.assertEqual(self.ping("kale", self.kale), 200)
        (a,) = self.alerts()
        self.assertEqual((a["kind"], a["vault"]), ("static_token_misuse", "Fern Cortex"))
        self.assertNotIn(self.fern, json.dumps(a))
        self.assertNotIn(new, json.dumps(a))

    def test_five_unknown_bearers_from_one_address_get_429_but_never_a_good_token(self):
        for i in range(4):
            self.assertEqual(self.ping("fern", f"guess-{i}"), 401)
        self.now += 601
        self.assertEqual(self.ping("fern", "guess-4"), 401, "the window slides: four of them are too old")
        self.assertEqual(self.ping("fern", None), 401, "no bearer at all is not counted (OAuth discovery)")
        for i in range(4):
            self.assertEqual(self.ping("fern", f"again-{i}"), 401)
        status, _, headers = self.call("/mcp/fern", PING, {"Authorization": "Bearer guess-more"})
        self.assertEqual(status, 429, "the address is blocked for unknown tokens")
        self.assertIn("Retry-After", headers)
        self.assertEqual(self.ping("fern", self.fern), 200, "a good agent sharing the address is never locked out")
        _, pair = self.flow()
        self.assertEqual(self.ping("fern", pair["access_token"]), 200)
        self.assertEqual([a["kind"] for a in self.alerts()], ["bad_bearer_block"])
        self.assertEqual(cortex_mcp.token_path(self.fern_v).read_text(encoding="utf-8").strip(), self.fern,
                         "nothing is revoked")
        self.now += 901
        self.assertEqual(self.ping("fern", "guess-5"), 401, "15 minutes later: counted again, not refused")

    def test_a_trusted_proxys_forwarded_address_is_used_an_untrusted_one_ignored(self):
        self.cls.trusted_proxies = hub_oauth.parse_proxies(["10.9.9.9", "127.0.0.0/8"])
        for i in range(5):
            self.ping("fern", f"guess-{i}", {"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.7"})
        self.assertEqual(self.ping("fern", "guess-x", {"X-Forwarded-For": "203.0.113.7"}), 429)
        self.assertEqual(self.ping("fern", self.fern, {"X-Forwarded-For": "203.0.113.7"}), 200, "its good token")
        self.assertEqual(self.ping("fern", "guess-x", {"X-Forwarded-For": "203.0.113.8"}), 401, "another client")
        self.assertEqual(self.ping("fern", "guess-x"), 401, "the proxy itself is not blocked")
        self.cls.trusted_proxies = []
        for i in range(4):
            self.ping("kale", f"guess-{i}", {"X-Forwarded-For": f"198.51.100.{i}"})
        self.assertEqual(self.ping("kale", "guess-y", {"X-Forwarded-For": "198.51.100.99"}), 429,
                         "an untrusted peer's X-Forwarded-For is ignored: the peer is blocked")
        trusted = hub_oauth.parse_proxies(["10.0.0.0/8,192.168.1.1"])
        self.assertEqual(hub_oauth.client_address("10.1.2.3", ["1.2.3.4, 5.6.7.8"], trusted), "5.6.7.8")
        self.assertEqual(hub_oauth.client_address("192.168.1.1", ["[2001:db8::1]:443"], trusted), "2001:db8::1")
        self.assertEqual(hub_oauth.client_address("192.168.1.2", ["5.6.7.8"], trusted), "192.168.1.2")
        self.assertEqual(hub_oauth.client_address("::ffff:10.0.0.1", ["garbage"], trusted), "10.0.0.1")


class LimiterTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.lim = hub_oauth.Limiter(lambda: self.now)

    def test_ipv6_is_counted_by_its_64_and_alerts_are_coalesced(self):
        hits = [self.lim.fail(f"2001:db8:1:2::{i}") for i in range(5)]
        self.assertEqual(hits, [False] * 4 + [True])
        self.assertGreater(self.lim.wait("2001:db8:1:2::99"), 0, "the whole /64 is one client")
        self.assertEqual(self.lim.wait("2001:db8:1:3::1"), 0)
        self.assertEqual(self.lim.wait("198.51.100.1"), 0)
        self.now += 901
        self.assertEqual(self.lim.wait("2001:db8:1:2::1"), 0)
        self.lim.alerted["2001:db8:1:2::/64"] = self.now   # alerted a moment ago
        self.assertEqual([self.lim.fail("2001:db8:1:2::1") for _ in range(5)], [False] * 5, "one alert a minute")
        self.assertGreater(self.lim.wait("2001:db8:1:2::1"), 0, "blocked all the same")

    def test_the_tables_stay_bounded(self):
        with mock.patch.object(hub_oauth, "MAX_TRACKED", 50):
            for i in range(400):
                for _ in range(5):
                    self.lim.fail(f"10.0.{i // 250}.{i % 250}")
                self.now += 1
            self.assertLessEqual(len(self.lim.blocked) + len(self.lim.fails) + len(self.lim.alerted), 3 * 50 + 3)

    def test_the_webhook_never_runs_more_than_a_few_posts_at_once(self):
        started = []
        with mock.patch.dict(os.environ, {"CORTEX_ALERT_WEBHOOK": "http://127.0.0.1:9/hook"}), \
                mock.patch.object(hub_oauth.threading, "Thread") as thread:
            thread.side_effect = lambda **k: started.append(k) or mock.Mock()
            for _ in range(20):
                hub_oauth.alert("test", None, "burst")
        self.assertEqual(len(started), hub_oauth.MAX_HOOKS)
        for _ in started:
            hub_oauth._hooks.release()


class HardeningTest(Base):
    host = "cortex.example.com"
    extra = {"scope": "lan", "domain": "athena.cortex.home", "key": "the-access-key", "lan_domain": "athena.cortex.home",
             "allowed": {"athena.cortex.home", "cortex.example.com"}, "lan_names": ["cortex.example.com"]}

    def test_the_sign_in_never_sends_back_a_cr_or_lf(self):
        evil = "/oauth/authorize?a=1\r\nSet-Cookie: pwned=1"
        self.assertEqual(cortex_hub.login_next(evil), "/oauth/authorize?a=1%0D%0ASet-Cookie%3A+pwned%3D1")
        self.assertEqual(cortex_hub.login_next("/oauth/authorizeX?a=1"), "")
        self.assertEqual(cortex_hub.login_next("//evil.example/oauth/authorize"), "")
        status, _, headers = self.call("/login", urlencode({"key": "the-access-key", "next": evil}).encode())
        self.assertEqual(status, 302)
        self.assertNotIn("pwned", "".join(f"{k}{v}" for k, v in headers.items() if k != "Location"))
        self.assertNotIn("\n", headers["Location"])
        status, page, _ = self.call("/login?next=" + quote(evil, safe=""))
        self.assertNotIn("\r\nSet-Cookie", page)

    def test_send_refuses_a_header_value_with_a_line_break(self):
        h = self.cls.__new__(self.cls)
        h.wfile, h.request_version, h.command, h.requestline = io.BytesIO(), "HTTP/1.1", "GET", "GET / HTTP/1.1"
        h.client_address, h.headers = ("127.0.0.1", 1), Message()
        h.redirect("/ok\r\nSet-Cookie: pwned=1")
        out = h.wfile.getvalue()
        self.assertIn(b" 500 ", out.split(b"\r\n")[0], out[:40])
        self.assertNotIn(b"pwned", out)

    def test_a_negative_or_bad_content_length_is_refused(self):
        port = self.httpd.server_address[1]
        for path, auth in (("/oauth/register", ""), ("/oauth/token", ""), ("/mcp/kale", f"Authorization: Bearer {self.kale}\r\n"),
                           ("/login", "")):
            for length in ("-1", "abc"):
                with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                    sock.sendall(f"POST {path} HTTP/1.1\r\nHost: {self.host}\r\n{auth}Content-Type: application/json\r\n"
                                 f"Content-Length: {length}\r\nConnection: close\r\n\r\n".encode())
                    got = b""
                    while chunk := sock.recv(4096):   # the whole answer: the hub closes the connection after it
                        got += chunk
                first = got.split(b"\r\n")[0]
                self.assertIn(b" 400 ", first + b" ", (path, length))


class RegistrationLimitTest(Base):
    def test_redirect_uris_with_control_characters_or_spaces_are_refused(self):
        for uri in ("http://localhost:1/cb\r\nSet-Cookie: x=1", "http://localhost/c b", "http://localhost/c\tb",
                    "https://agent.example/cb\x7f", "https://agent.example/cb?", "https://agënt.example/cb"):
            self.assertFalse(hub_oauth.redirect_ok(uri), repr(uri))
            status, body, _ = self.call("/oauth/register", json.dumps({"redirect_uris": [uri]}).encode(),
                                        {"Content-Type": "application/json"})
            self.assertEqual(status, 400, repr(uri))
        self.assertFalse(hub_oauth.redirect_matches([REDIRECT], "http://localhost:5/callback\r\nX: y"))
        self.assertTrue(hub_oauth.redirect_matches([REDIRECT], "http://localhost:5/callback"))

    def test_registration_is_rate_limited_per_address(self):
        for _ in range(hub_oauth.REG_LIMIT):
            self.register()
        status, body, headers = self.call("/oauth/register", json.dumps({"redirect_uris": [REDIRECT]}).encode(),
                                          {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["error"]), (429, "slow_down"))
        self.assertIn("Retry-After", headers)
        self.now += hub_oauth.REG_BLOCK + 1
        self.register()

    def test_a_full_registry_evicts_the_oldest_never_approved_client(self):
        self.cls.reg_limiter = None
        with mock.patch.object(hub_oauth, "MAX_CLIENTS", 3):
            approved, _ = self.flow()
            self.now += 1
            first = self.register("first")
            self.now += 1
            second = self.register("second")
            self.now += 1
            third = self.register("third")
            self.assertIsNone(self.store.client(first), "the oldest waiting client made room")
            self.assertIsNotNone(self.store.client(approved), "an approved client is never evicted")
            self.assertIsNotNone(self.store.client(second))
            self.assertIsNotNone(self.store.client(third))
        self.now += hub_oauth.CLIENT_UNUSED + 1
        self.register("later")
        self.assertIsNone(self.store.client(second), "never approved: gone after 10 minutes")
        self.assertIsNotNone(self.store.client(approved))


class OpenRedirectTest(Base):
    def test_errors_go_back_only_to_a_loopback_redirect_or_after_the_owner_acted(self):
        cid = self.register(uris=("https://agent.example/cb",))
        _, challenge = pkce()
        p = self.params(cid, challenge, redirect_uri="https://agent.example/cb", code_challenge_method="plain")
        status, _, headers = self.call("/oauth/authorize?" + urlencode(p))
        self.assertEqual(status, 400, "an error page, not a redirect to whatever https URI was registered")
        self.assertNotIn("Location", headers)
        self.assertEqual(self.approve(p)[0], 400)
        p = self.params(cid, challenge, redirect_uri="https://agent.example/cb")
        status, _, headers = self.approve(p, "deny")
        self.assertEqual(status, 302, "the owner denied it: the client is told")
        self.assertTrue(headers["Location"].startswith("https://agent.example/cb?error=access_denied"))


class AlertTest(unittest.TestCase):
    def test_an_alert_is_a_json_line_and_a_webhook_post(self):
        got, done = [], threading.Event()

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()
                done.set()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = os.environ.get("CORTEX_ALERT_WEBHOOK")
        with tempfile.TemporaryDirectory() as tmp:
            hub_oauth.DATA = Path(tmp)
            os.environ["CORTEX_ALERT_WEBHOOK"] = f"http://127.0.0.1:{srv.server_address[1]}/hook"
            try:
                line = hub_oauth.alert("test", "Fern Cortex", "a detail")
                self.assertTrue(done.wait(5))
                self.assertEqual(got, [line])
                self.assertEqual(json.loads((Path(tmp) / "alerts.jsonl").read_text(encoding="utf-8")), line)
            finally:
                hub_oauth.DATA = None
                if old is None:
                    os.environ.pop("CORTEX_ALERT_WEBHOOK", None)
                else:
                    os.environ["CORTEX_ALERT_WEBHOOK"] = old
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
