#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Agent sessions for the hub's MCP service: an OAuth 2.1 authorization server, as the MCP authorization spec defines
it, so Claude Code, Codex, Gemini CLI and OpenCode handle their tokens themselves; and abuse checks on static tokens.

Routes (the Cortex hub mixes OAuthRoutes in): GET /.well-known/oauth-protected-resource[/mcp/<vault>] (RFC 9728) and
/.well-known/oauth-authorization-server (RFC 8414) · POST /oauth/register (RFC 7591, public clients only) ·
GET /oauth/authorize, the owner's approval page (PKCE S256 required, `resource` naming one vault, RFC 8707), and its
POST · POST /oauth/token (authorization_code, refresh_token) · GET /api/oauth and POST /api/oauth/<id>/revoke for the
owner.

A session (a "family") is one client's grant on one vault. Its access token expires after 60 minutes without use (each
accepted MCP call extends it) and 24 hours after it was issued in any case; each refresh returns a new pair and the
presented refresh token stops working. The previous refresh token presented again within 30 s gets the same pair back
(a client retrying a lost answer); later, or an older one, means it leaked: the whole session is revoked and an alert
written. Tokens are kept only as SHA-256 hashes in DATA/oauth.json, never logged.

Static tokens (_brain/state/mcp.token, for agents that send a fixed header): one vault's token presented at another
vault is rotated, with an alert; a client address that sends 5 bearers matching nothing within 10 minutes gets 429 for
15 minutes. Alerts are JSON lines in DATA/alerts.jsonl, also POSTed to CORTEX_ALERT_WEBHOOK when it is set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

import cortex_mcp

ACCESS_IDLE = 3600            # an access token unused this long is dead
ACCESS_CAP = 24 * 3600        # and any access token this old
CODE_LIFE = 60                # an authorization code, single use
REFRESH_GRACE = 30            # the just-rotated refresh token may be retried this long and gets the same pair
REFRESH_LIFE = 30 * 86400     # a session not refreshed for this long ends
REVOKED_KEEP = 7 * 86400      # a revoked session stays listed (and its tokens recognised) this long
CLIENT_UNUSED = 600           # a client registered but never approved is forgotten after 10 minutes
MAX_CLIENTS = 500             # when full, the oldest never-approved client makes room
SAVE_EVERY = 60               # the last-use stamps of accepted calls reach the disk at most this often
FAIL_LIMIT, FAIL_WINDOW, BLOCK_FOR = 5, 600, 900   # 5 unknown bearers in 10 minutes: 429 for 15 minutes
REG_LIMIT, REG_WINDOW, REG_BLOCK = 10, 600, 600    # 10 registrations in 10 minutes from one address: 429 for 10
ALERT_EVERY = 60              # at most one bad_bearer_block alert per address in this many seconds
MAX_TRACKED = 10000           # addresses a limiter keeps at most
MAX_HOOKS = 4                 # webhook posts in flight at once; more are dropped (the alert line is still written)
WELL_KNOWN_PR = "/.well-known/oauth-protected-resource"
WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
LOOPBACK = ("localhost", "127.0.0.1", "::1")
AUTHORIZE_FIELDS = ("client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method",
                    "resource", "state", "scope")
DATA: Path | None = None      # the hub's data folder: alerts.jsonl goes there
_alert_lock = threading.Lock()
_hooks = threading.BoundedSemaphore(MAX_HOOKS)


def sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def stamp(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


# ---- alerts: a JSON line per event, and the webhook when one is set; never a token value
def _post_hook(url: str, line: dict) -> None:
    try:
        req = urllib.request.Request(url, json.dumps(line).encode("utf-8"), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read(0)
    except Exception as e:   # the hook is a courtesy: its failure is one line, never the caller's problem
        sys.stderr.write(f"alert webhook failed: {type(e).__name__}\n")
    finally:
        _hooks.release()


def alert(kind: str, vault: str | None, detail: str) -> dict:
    """Appends {t, kind, vault, detail} to DATA/alerts.jsonl and POSTs it to CORTEX_ALERT_WEBHOOK in the background."""
    line = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, "vault": vault, "detail": detail}
    sys.stderr.write(f"alert {kind} {vault or '-'}: {detail}\n")
    if DATA is not None:
        try:
            with _alert_lock:
                DATA.mkdir(parents=True, exist_ok=True)
                fd = os.open(DATA / "alerts.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            sys.stderr.write(f"alert not written to {DATA}: {type(e).__name__}\n")
    hook = os.environ.get("CORTEX_ALERT_WEBHOOK", "").strip()
    if hook:
        if _hooks.acquire(blocking=False):   # a burst of alerts never becomes a burst of threads
            threading.Thread(target=_post_hook, args=(hook, line), daemon=True).start()
        else:
            sys.stderr.write("alert webhook skipped: too many in flight\n")
    return line


# ---- redirect URIs and PKCE
def clean_uri(uri: str) -> bool:
    """Printable ASCII only (no space, no control character: it ends up in a Location header) and written the way
    urlsplit reads it back, so what was checked is what is sent."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000 or not all("\x21" <= c <= "\x7e" for c in uri):
        return False
    try:
        s = urlsplit(uri)
        s.port   # a malformed port raises here
    except ValueError:
        return False
    return urlunsplit(s) == uri


def loopback(uri: str) -> bool:
    s = urlsplit(uri)
    return s.scheme == "http" and s.hostname in LOOPBACK


def redirect_ok(uri: str) -> bool:
    """A redirect URI a public client may register: http on a loopback address, or https; no fragment."""
    if not clean_uri(uri) or "#" in uri:
        return False
    s = urlsplit(uri)
    return bool(s.hostname) and (s.scheme == "https" or loopback(uri))


def redirect_matches(registered: list, given: str) -> bool:
    """Exactly one of the registered URIs; on a loopback address any port (OAuth 2.1: the client picks it per run)."""
    if not clean_uri(given) or "#" in given:
        return False
    if given in registered:
        return True
    g = urlsplit(given)
    if not loopback(given):
        return False
    for r in registered:
        s = urlsplit(r)
        if s.scheme == "http" and s.hostname == g.hostname and s.path == g.path and s.query == g.query:
            return True
    return False


def pkce_ok(verifier: str | None, challenge: str) -> bool:
    if not verifier or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        return False
    got = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return hmac.compare_digest(got, challenge.encode("ascii"))


def with_query(uri: str, params: dict) -> str:
    return uri + ("&" if urlsplit(uri).query else ("" if uri.endswith("?") else "?")) + urlencode(params)


# ---- client addresses, for the bad-bearer limit
def parse_proxies(items) -> list:
    """CORTEX_TRUSTED_PROXIES / --trusted-proxy: addresses or networks, comma separated."""
    out = []
    for item in items or []:
        for x in str(item).split(","):
            if x.strip():
                try:
                    out.append(ipaddress.ip_network(x.strip(), strict=False))
                except ValueError:
                    sys.exit(f"trusted proxy {x.strip()}: an address or a network (10.0.0.0/8)")
    return out


def _ip(s: str):
    s = (s or "").strip()
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        m = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::\d+)?|(\d+\.\d+\.\d+\.\d+):\d+", s)
        if not m:
            return None
        try:
            ip = ipaddress.ip_address(m.group(1) or m.group(2))
        except ValueError:
            return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def limit_key(addr: str) -> str:
    """What a limiter counts: the address, an IPv6 one by its /64 (one host owns the whole block)."""
    ip = _ip(addr)
    if ip is not None and ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip) if ip is not None else addr


def client_address(peer: str, forwarded: list, trusted: list) -> str:
    """The socket peer, or the last X-Forwarded-For hop when the peer is a trusted proxy."""
    ip = _ip(peer)
    if ip is None:
        return peer
    if any(ip.version == n.version and ip in n for n in trusted):
        hops = [h.strip() for x in forwarded for h in str(x).split(",") if h.strip()]
        last = _ip(hops[-1]) if hops else None
        if last is not None:
            return str(last)
    return str(ip)


class Limiter:
    """Events per client address (an IPv6 one by its /64): `limit` of them within `window` seconds block it for
    `block` seconds. By default the unknown bearers: FAIL_LIMIT in FAIL_WINDOW block for BLOCK_FOR."""

    def __init__(self, clock=time.time, limit: int = FAIL_LIMIT, window: float = FAIL_WINDOW, block: float = BLOCK_FOR):
        self.clock, self.limit, self.window, self.block = clock, limit, window, block
        self.lock = threading.Lock()
        self.fails: dict[str, deque] = {}
        self.blocked: dict[str, float] = {}
        self.alerted: dict[str, float] = {}

    def wait(self, addr: str) -> int:
        """Seconds this address must still wait; 0 when it may go on."""
        key = limit_key(addr)
        with self.lock:
            until = self.blocked.get(key)
            if until is None:
                return 0
            left = until - self.clock()
            if left <= 0:
                del self.blocked[key]
                return 0
            return int(left) + 1

    def prune(self, now: float) -> None:
        for d, alive in ((self.fails, lambda q: q and now - q[-1] <= self.window),
                         (self.blocked, lambda until: until > now),
                         (self.alerted, lambda t: now - t < ALERT_EVERY)):
            for k in [k for k, x in d.items() if not alive(x)]:
                del d[k]
            while len(d) > MAX_TRACKED:   # a scan from very many addresses: the oldest entries go first
                del d[next(iter(d))]

    def fail(self, addr: str) -> bool:
        """Counts one event; True when that blocks the address and no alert was raised for it in the last minute."""
        key = limit_key(addr)
        with self.lock:
            now = self.clock()
            if len(self.fails) + len(self.blocked) + len(self.alerted) > MAX_TRACKED:
                self.prune(now)
            q = self.fails.setdefault(key, deque())
            q.append(now)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) < self.limit:
                return False
            self.blocked[key] = now + self.block
            del self.fails[key]
            if now - self.alerted.get(key, -ALERT_EVERY) < ALERT_EVERY:
                return False
            self.alerted[key] = now
            return True


# ---- the store: clients, codes and sessions, hashes only
class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


class Store:
    """DATA/oauth.json: {clients, codes, families}. Every change under one lock and written atomically (tmp + replace,
    mode 600); a token is kept only as its SHA-256. The pair a refresh just issued stays in memory for the retry
    grace only. path None keeps everything in memory (a hub with no --data)."""

    def __init__(self, path: Path | None, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()
        self.grace: dict[str, tuple[str, dict, float]] = {}   # family -> (previous refresh hash, the pair, until)
        self.saved_at = 0.0
        self.db: dict[str, dict] = {"clients": {}, "codes": {}, "families": {}}
        if path is not None and path.is_file():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
                for k in self.db:
                    if isinstance(d.get(k), dict):
                        self.db[k] = d[k]
            except (OSError, ValueError):
                sys.stderr.write(f"oauth: {path} unreadable, starting with no sessions\n")

    def save(self) -> None:
        self.saved_at = self.clock()
        if self.path is None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.db, f, indent=1)
        os.replace(tmp, self.path)

    def prune(self, now: float) -> None:
        db = self.db
        for h in [h for h, c in db["codes"].items() if c.get("expires", 0) < now]:
            del db["codes"][h]
        for fid in list(db["families"]):
            f = db["families"][fid]
            if (f.get("state") == "revoked" and f.get("revoked", 0) + REVOKED_KEEP < now) or \
                    f.get("rotated", 0) + REFRESH_LIFE < now:
                del db["families"][fid]
                continue
            for h in [h for h, a in f.get("access", {}).items() if a.get("issued", 0) + ACCESS_CAP < now]:
                del f["access"][h]
        used = self.approved()
        for cid in [c for c, x in db["clients"].items() if c not in used and x.get("created", 0) + CLIENT_UNUSED < now]:
            del db["clients"][cid]
        for fid in [f for f, g in self.grace.items() if g[2] < now]:
            del self.grace[fid]

    def approved(self) -> set:
        """The clients the owner approved: with a session or a code not yet exchanged."""
        return {f.get("client") for f in self.db["families"].values()} | {c.get("client") for c in self.db["codes"].values()}

    def client(self, cid: str | None) -> dict | None:
        with self.lock:
            return self.db["clients"].get(cid or "")

    def register(self, meta) -> dict:
        if not isinstance(meta, dict):
            raise OAuthError("invalid_client_metadata", "the registration is a JSON object")
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 10 or \
                not all(isinstance(u, str) and redirect_ok(u) for u in uris):
            raise OAuthError("invalid_redirect_uri",
                             "redirect_uris: http on localhost, 127.0.0.1 or [::1], or https; no fragment")
        grants = meta.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grants, list) or "authorization_code" not in grants or \
                not set(grants) <= {"authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", "grant_types: authorization_code and refresh_token")
        if (meta.get("response_types") or ["code"]) != ["code"]:
            raise OAuthError("invalid_client_metadata", "response_types: code")
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(meta.get("client_name") or "an agent")).strip()[:80] or "an agent"
        with self.lock:
            now = self.clock()
            self.prune(now)
            if len(self.db["clients"]) >= MAX_CLIENTS:   # full: the oldest never-approved client makes room
                used = self.approved()
                waiting = sorted((x.get("created", 0), c) for c, x in self.db["clients"].items() if c not in used)
                if not waiting:
                    raise OAuthError("invalid_client_metadata", "too many approved clients: revoke some first")
                del self.db["clients"][waiting[0][1]]
            cid = secrets.token_urlsafe(16)
            self.db["clients"][cid] = {"name": name, "redirect_uris": uris, "grant_types": grants, "created": now}
            self.save()
        return {"client_id": cid, "client_id_issued_at": int(now), "client_name": name, "redirect_uris": uris,
                "grant_types": grants, "response_types": ["code"], "token_endpoint_auth_method": "none"}

    def new_code(self, cid: str, redirect_uri: str, challenge: str, vault: str, resource: str) -> str:
        code = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self.prune(now)
            self.db["codes"][sha(code)] = {"client": cid, "redirect_uri": redirect_uri, "challenge": challenge,
                                           "vault": vault, "resource": resource, "expires": now + CODE_LIFE,
                                           "used": None}
            self.save()
        return code

    def _issue(self, fam: dict, now: float) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        fam["access"] = {sha(access): {"issued": now, "used": now}}   # one live access token per session
        fam["refresh"], fam["rotated"] = sha(refresh), now
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_IDLE, "refresh_token": refresh}

    def _revoke(self, fid: str, now: float) -> dict | None:
        fam = self.db["families"].get(fid)
        if fam is not None and fam.get("state") == "active":
            fam["state"], fam["revoked"] = "revoked", now
            self.grace.pop(fid, None)
        return fam

    def revoke(self, fid: str) -> bool:
        with self.lock:
            fam = self._revoke(fid, self.clock())
            if fam is None:
                return False
            self.save()
            return True

    def exchange(self, form: dict, vault_for) -> dict:
        """grant_type=authorization_code: the code once, its PKCE verifier, the same client and redirect_uri."""
        cid, code = form.get("client_id"), form.get("code")
        with self.lock:
            now = self.clock()
            self.prune(now)
            if self.db["clients"].get(cid or "") is None:
                raise OAuthError("invalid_client", "unknown client_id: register first", 401)
            if not code:
                raise OAuthError("invalid_request", "code is required")
            h = sha(code)
            rec = self.db["codes"].get(h)
            if rec is None or rec["expires"] < now:
                raise OAuthError("invalid_grant", "unknown or expired code")
            if rec.get("used"):   # a code replayed: what it gave is no longer trusted
                fam = self._revoke(rec["used"], now)
                self.save()
                if fam is not None:
                    alert("code_reuse", fam["vault"], f"an authorization code of {fam['client_name']} was presented "
                                                      "twice; its session was revoked")
                raise OAuthError("invalid_grant", "this code was already used")
            if not hmac.compare_digest(rec["client"].encode("utf-8"), cid.encode("utf-8")) or rec["redirect_uri"] != form.get("redirect_uri"):
                del self.db["codes"][h]
                self.save()
                raise OAuthError("invalid_grant", "the code was issued to another client or redirect_uri")
            if not pkce_ok(form.get("code_verifier"), rec["challenge"]):
                del self.db["codes"][h]
                self.save()
                raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge")
            if form.get("resource") and vault_for(form["resource"]) != rec["vault"]:
                raise OAuthError("invalid_target", "resource is not the cortex this code was approved for")
            fid = secrets.token_urlsafe(12)
            fam = {"client": cid, "client_name": self.db["clients"][cid]["name"], "vault": rec["vault"],
                   "created": now, "used": now, "state": "active", "previous": None, "old": []}
            pair = self._issue(fam, now)
            self.db["families"][fid] = fam
            rec["used"] = fid
            self.save()
            return pair

    def refresh(self, form: dict, vault_for) -> dict:
        """grant_type=refresh_token: rotation, the retry grace and reuse detection (see the module's docstring)."""
        cid, tok = form.get("client_id"), form.get("refresh_token")
        if not tok:
            raise OAuthError("invalid_request", "refresh_token is required")
        h = sha(tok)
        with self.lock:
            now = self.clock()
            self.prune(now)
            found = None
            for fid, fam in self.db["families"].items():
                if fam.get("refresh") == h:
                    found = (fid, fam, "current")
                elif fam.get("previous") == h:
                    found = (fid, fam, "previous")
                elif h in fam.get("old", []):
                    found = (fid, fam, "old")
                if found:
                    break
            if found is None:
                raise OAuthError("invalid_grant", "unknown refresh token")
            fid, fam, which = found
            if cid and not hmac.compare_digest(cid.encode("utf-8"), fam["client"].encode("utf-8")):
                raise OAuthError("invalid_grant", "this refresh token belongs to another client")
            if fam.get("state") != "active":
                raise OAuthError("invalid_grant", "this session was revoked: authorize again")
            if form.get("resource") and vault_for(form["resource"]) != fam["vault"]:
                raise OAuthError("invalid_target", "resource is not this session's cortex")
            if which == "current":
                if fam["previous"]:
                    fam["old"] = (fam["old"] + [fam["previous"]])[-500:]   # the older refresh tokens, for reuse detection
                fam["previous"] = h
                pair = self._issue(fam, now)
                fam["used"] = now
                self.grace[fid] = (h, dict(pair), now + REFRESH_GRACE)
                self.save()
                return pair
            if which == "previous" and now - fam["rotated"] <= REFRESH_GRACE:
                g = self.grace.get(fid)
                if g is not None and hmac.compare_digest(g[0], h):
                    return dict(g[1])   # a retry of a refresh whose answer was lost: the same pair
                raise OAuthError("invalid_grant", "this refresh token was just rotated: authorize again")
            self._revoke(fid, now)
            self.save()
            alert("refresh_reuse", fam["vault"], f"a used refresh token of {fam['client_name']} was presented again; "
                                                 "its session was revoked")
            raise OAuthError("invalid_grant", "this refresh token was already used: the session is revoked")

    def check_access(self, token: str, vault: str) -> str | None:
        """ok (and the idle timeout slides), expired, revoked, wrong_vault (the session is revoked), or None when the
        token is no access token at all."""
        h = sha(token)
        with self.lock:
            now = self.clock()
            for fid, fam in self.db["families"].items():
                a = fam.get("access", {}).get(h)
                if a is None:
                    continue
                if fam["vault"] != vault:
                    if fam.get("state") == "active":
                        self._revoke(fid, now)
                        self.save()
                        alert("oauth_wrong_vault", fam["vault"], f"an access token of {fam['client_name']} for "
                                                                 f"{fam['vault']} was presented at {vault}; its session "
                                                                 "was revoked")
                    return "wrong_vault"
                if fam.get("state") != "active":
                    return "revoked"
                if now - a["used"] > ACCESS_IDLE or now - a["issued"] > ACCESS_CAP:
                    return "expired"
                a["used"] = fam["used"] = now
                if now - self.saved_at >= SAVE_EVERY:
                    self.save()
                return "ok"
            return None

    def sessions(self) -> list[dict]:
        with self.lock:
            now = self.clock()
            out = []
            for fid, fam in self.db["families"].items():
                state = fam.get("state", "active")
                if state == "active" and fam.get("rotated", 0) + REFRESH_LIFE < now:
                    state = "expired"
                elif state == "active" and not any(now - a["used"] <= ACCESS_IDLE and now - a["issued"] <= ACCESS_CAP
                                                   for a in fam.get("access", {}).values()):
                    state = "idle"   # the client refreshes on its next call
                out.append({"id": fid, "vault": fam["vault"], "client": fam.get("client_name"),
                            "created": stamp(fam.get("created")), "last_used": stamp(fam.get("used")), "state": state})
            return sorted(out, key=lambda x: (x["vault"], x["created"] or ""))


def metadata(base: str) -> dict:
    """RFC 8414: what an MCP client reads to find the endpoints."""
    return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True}


class Refusal(Exception):
    """An authorize request refused: back to the client when its redirect_uri is known, else a page."""

    def __init__(self, redirect: str | None, error: str, description: str, state: str | None = None):
        super().__init__(description)
        self.redirect, self.error, self.description, self.state = redirect, error, description, state


APPROVE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cortex hub</title>
<style>
:root { color-scheme: light dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText;
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
form { display: grid; gap: 10px; width: min(420px, calc(100vw - 32px)); }
h1 { font-size: 20px; margin: 0; }
p { margin: 0; }
.note { color: GrayText; font-size: 12px; }
.row { display: flex; gap: 8px; }
button { font: inherit; padding: 7px 14px; border-radius: 8px; border: 1px solid GrayText; cursor: pointer; }
</style>
</head>
<body>
<form method="post" action="/oauth/authorize">
  <h1>Connect an agent</h1>
  <p><b>__CLIENT__</b> asks to use <b>__VAULT__</b> over MCP: to read it and to add what it learns.</p>
  <p class="note">It goes back to __TARGET__. Its session ends after 60 minutes without use and renews itself while
  the agent works; revoke it any time from the hub.</p>
  __FIELDS__
  <div class="row"><button type="submit" name="decision" value="approve">Approve</button>
  <button type="submit" name="decision" value="deny">Deny</button></div>
</form>
</body>
</html>
"""


class OAuthRoutes:
    """The hub's OAuth routes and its MCP gate. Needs the Cortex hub's handler (send, redirect, mcp_base, is_owner,
    signed_in, csrf, allowed, lan_names, mcp_hub, mcp_allows); off (exactly the static token, as before) while oauth
    is None."""
    oauth: Store | None = None
    limiter: Limiter | None = None       # unknown bearers per address
    reg_limiter: Limiter | None = None   # registrations per address
    trusted_proxies: list = []

    def peer(self) -> str:
        return client_address(self.client_address[0], self.headers.get_all("X-Forwarded-For") or [],
                              self.trusted_proxies)

    def oauth_on(self) -> bool:
        return self.oauth is not None and self.mcp_hub is not None

    def oauth_public(self, path: str) -> bool:
        """What agents reach without signing in: discovery, registration and the token endpoint."""
        return self.oauth_on() and (path.startswith(WELL_KNOWN_PR) or path.startswith(WELL_KNOWN_AS)
                                    or path in ("/oauth/register", "/oauth/token"))

    def oauth_json(self, code: int, obj, headers: dict | None = None):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json",
                  {"Pragma": "no-cache", **(headers or {})})

    def vault_for(self, resource: str) -> Path | None:
        """The vault a resource indicator names: <this hub>/mcp/<vault>, served on this listener."""
        try:
            s = urlsplit(resource or "")
            s.port
        except ValueError:
            return None
        if s.scheme not in ("http", "https") or "#" in resource:
            return None
        hosts = set(self.allowed) | set(self.lan_names) | {urlsplit(self.mcp_base()).hostname}
        if (s.hostname or "") not in hosts:
            return None
        parts = [unquote(x) for x in s.path.strip("/").split("/")]
        if len(parts) != 2 or parts[0] != "mcp":
            return None
        v = self.mcp_hub.vaults().get(parts[1])
        return v if v is not None and self.mcp_allows(v.name) else None

    def vault_name_for(self, resource: str) -> str | None:
        v = self.vault_for(resource)
        return v.name if v is not None else None

    # -- GET
    def oauth_get(self, path: str) -> bool:
        """Answers the OAuth GET routes; False when the path is none of them."""
        if not self.oauth_on():
            return False
        base = self.mcp_base()
        if path == WELL_KNOWN_AS or path.startswith(WELL_KNOWN_AS + "/"):
            self.oauth_json(200, metadata(base))
            return True
        if path == WELL_KNOWN_PR:
            self.oauth_json(200, {"resource": base, "authorization_servers": [base],
                                  "bearer_methods_supported": ["header"]})
            return True
        if path.startswith(WELL_KNOWN_PR + "/"):
            rest = path[len(WELL_KNOWN_PR):].rstrip("/")
            parts = [unquote(x) for x in rest.strip("/").split("/")]
            v = self.mcp_hub.vaults().get(parts[1]) if len(parts) == 2 and parts[0] == "mcp" else None
            if v is None or not self.mcp_allows(v.name):
                self.oauth_json(404, {"error": "no such cortex here"})
                return True
            self.oauth_json(200, {"resource": base + rest, "authorization_servers": [base],
                                  "bearer_methods_supported": ["header"], "resource_name": v.name})
            return True
        if path == "/oauth/authorize":
            self.authorize_page()
            return True
        if path == "/api/oauth":
            if not self.is_owner():
                self.send(403, b"the hub's owner only", "text/plain; charset=utf-8")
                return True
            shown = {v.name for v in self.mcp_hub.vaults().values() if self.mcp_allows(v.name)}
            self.oauth_json(200, {"sessions": [s for s in self.oauth.sessions() if s["vault"] in shown]})
            return True
        return False

    def authorize_check(self, q: dict) -> tuple[dict, Path]:
        cid, ruri, state = q.get("client_id") or "", q.get("redirect_uri") or "", q.get("state")
        client = self.oauth.client(cid)
        if client is None:
            raise Refusal(None, "invalid_client", "unknown client_id: the agent registers first (POST /oauth/register)")
        if not redirect_matches(client["redirect_uris"], ruri):
            raise Refusal(None, "invalid_request", "redirect_uri is not one this client registered")
        if q.get("response_type") != "code":
            raise Refusal(ruri, "unsupported_response_type", "response_type=code", state)
        if q.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", q.get("code_challenge") or ""):
            raise Refusal(ruri, "invalid_request", "PKCE is required: code_challenge with code_challenge_method=S256", state)
        v = self.vault_for(q.get("resource") or "")
        if v is None:
            raise Refusal(ruri, "invalid_target", f"resource: one cortex's MCP URL, such as {self.mcp_base()}/mcp/<vault>",
                          state)
        return client, v

    def refuse(self, r: Refusal, owner_acted: bool = False):
        """An error goes back to the client only on a loopback redirect_uri, or once the owner acted (deny): an https
        redirect_uri anyone registered must not make the hub an open redirector."""
        if r.redirect is None or not (owner_acted or loopback(r.redirect)):
            return self.send(400, f"Cannot connect this agent: {r.description}.".encode("utf-8"),
                             "text/plain; charset=utf-8")
        params = {"error": r.error, "error_description": r.description, "iss": self.mcp_base()}
        if r.state is not None:
            params["state"] = r.state
        return self.redirect(with_query(r.redirect, params))

    def owner_or_refuse(self) -> bool:
        if self.is_owner():
            return True
        if self.scope == "lan" and self.key and not self.signed_in():
            self.redirect("/login?next=" + quote(self.path, safe=""))
        else:
            self.send(403, b"only the hub's owner approves agents", "text/plain; charset=utf-8")
        return False

    def authorize_page(self):
        if not self.owner_or_refuse():
            return
        q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            client, v = self.authorize_check(q)
        except Refusal as r:
            return self.refuse(r)
        esc = lambda s: html.escape(str(s), quote=True)
        fields = "".join(f'<input type="hidden" name="{k}" value="{esc(q[k])}">' for k in AUTHORIZE_FIELDS if k in q)
        fields += f'<input type="hidden" name="csrf" value="{esc(self.csrf)}">'
        target = urlsplit(q["redirect_uri"])
        page = (APPROVE.replace("__CLIENT__", esc(client["name"])).replace("__VAULT__", esc(v.name))
                .replace("__TARGET__", esc(f"{target.scheme}://{target.netloc}")).replace("__FIELDS__", fields))
        self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    # -- POST
    def oauth_post_public(self, path: str, raw: bytes):
        """POST /oauth/register and /oauth/token: agents, no browser, no sign-in."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if path == "/oauth/register" or ctype == "application/json":
                form = json.loads(raw.decode("utf-8") or "{}")
            else:
                form = {k: v[-1] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
        except (ValueError, UnicodeDecodeError):
            return self.oauth_json(400, {"error": "invalid_request", "error_description": "an unreadable body"})
        try:
            if path == "/oauth/register":
                addr = self.peer()
                wait = self.reg_limiter.wait(addr) if self.reg_limiter is not None else 0
                if wait:
                    return self.oauth_json(429, {"error": "slow_down", "error_description": "too many registrations "
                                                 "from this address: wait"}, {"Retry-After": str(wait)})
                out = self.oauth.register(form)
                if self.reg_limiter is not None:
                    self.reg_limiter.fail(addr)   # counts every registration, not only failures
                return self.oauth_json(201, out)
            if not isinstance(form, dict) or not all(isinstance(x, str) for x in form.values()):
                raise OAuthError("invalid_request", "form fields, each a string")
            grant = form.get("grant_type")
            if grant == "authorization_code":
                pair = self.oauth.exchange(form, self.vault_name_for)
            elif grant == "refresh_token":
                pair = self.oauth.refresh(form, self.vault_name_for)
            else:
                raise OAuthError("unsupported_grant_type", "grant_type: authorization_code or refresh_token")
        except OAuthError as e:
            return self.oauth_json(e.status, e.body())
        return self.oauth_json(200, pair)

    def oauth_post_owner(self, path: str, raw: bytes) -> bool:
        """The owner's POSTs (same origin already checked): the approval and a revoke. False when not one of them."""
        if not self.oauth_on():
            return False
        m = re.fullmatch(r"/api/oauth/([A-Za-z0-9_-]{1,64})/revoke", path)
        if path != "/oauth/authorize" and m is None:
            return False
        if path == "/oauth/authorize":
            form = {k: v[-1] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        else:
            form = {}
        if not self.owner_or_refuse():
            return True
        given = form.get("csrf") or self.headers.get("X-Cortex-Csrf") or ""
        if not hmac.compare_digest(given.encode("utf-8"), self.csrf.encode("utf-8")):
            self.send(403, b"stale page: reload it", "text/plain; charset=utf-8")
            return True
        if m is not None:
            ok = self.oauth.revoke(m.group(1))
            self.oauth_json(200 if ok else 404, {"message": "revoked: the agent must be approved again"} if ok
                            else {"error": "no such session"})
            return True
        try:
            client, v = self.authorize_check(form)
        except Refusal as r:
            self.refuse(r)
            return True
        ruri, state = form["redirect_uri"], form.get("state")
        if form.get("decision") != "approve":
            self.refuse(Refusal(ruri, "access_denied", "the hub's owner denied this agent", state), owner_acted=True)
            return True
        code = self.oauth.new_code(form["client_id"], ruri, form["code_challenge"], v.name, form["resource"])
        params = {"code": code, "iss": self.mcp_base(), **({"state": state} if state is not None else {})}
        self.redirect(with_query(ruri, params))
        return True

    # -- the MCP gate
    def mcp_refuse(self, v: Path, challenge: str) -> bool:
        self.mcp_send(401, cortex_mcp.failure(None, -32001, f"this cortex needs a session (OAuth) or its token: "
                                                            f"Authorization: Bearer <the line in {v.name}/"
                                                            f"{cortex_mcp.TOKEN_IGNORE}>"),
                      {"WWW-Authenticate": challenge})
        return True

    def mcp_denied(self, v: Path) -> bool:
        """An OAuth access token for this vault, or the vault's static token. A token of another vault is abuse: an
        OAuth session is revoked, a static token rotated, both with an alert. Unknown bearers count towards the
        address's limit, and a blocked address gets 429 for them; a valid token is checked first and always works, so
        an address shared with an attacker (a proxy, a NAT) never locks a good agent out."""
        if not self.oauth_on() or not self.mcp_require_token:
            return super().mcp_denied(v)
        auth = (self.headers.get("Authorization") or "").strip()
        path = quote(urlsplit(self.path).path, safe="/%:@!$&'()*+,;=-._~")
        challenge = f'Bearer resource_metadata="{self.mcp_base()}{WELL_KNOWN_PR}{path}"'
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            return self.mcp_refuse(v, challenge)
        if cortex_mcp.token_ok(v, auth, self.mcp_tokens):
            return False
        got = self.oauth.check_access(auth[7:].strip(), v.name)
        if got == "ok":
            return False
        if got is None:
            others = {x for x in self.mcp_hub.vaults().values() if x != v}
            owner = next((x for x in sorted(others) if cortex_mcp.token_ok(x, auth, self.mcp_tokens)), None)
            if owner is not None:
                cortex_mcp.rotate_token(owner)
                self.mcp_tokens.pop(owner, None)
                alert("static_token_misuse", owner.name, f"its static MCP token was presented at {v.name}: rotated, "
                                                         "the agents that use it need the new one")
            elif self.limiter is not None:
                addr = self.peer()
                wait = self.limiter.wait(addr)
                if wait:
                    self.mcp_send(429, cortex_mcp.failure(None, -32001, "too many unknown tokens from this address: "
                                                                        "wait"), {"Retry-After": str(wait)})
                    return True
                if self.limiter.fail(addr):
                    alert("bad_bearer_block", None, f"{limit_key(addr)}: {FAIL_LIMIT} unknown tokens within "
                                                    f"{FAIL_WINDOW // 60} minutes, refused for {BLOCK_FOR // 60} minutes")
        return self.mcp_refuse(v, challenge + ', error="invalid_token"')
