#!/usr/bin/env python3
"""Cortex hub: one web page with every agent's brain panel, live.

    python3 scripts/cortex_hub.py [--root ~/Vaults] [--host 127.0.0.1] [--port 8790]
    python3 scripts/cortex_hub.py --data ~/.cortex-hub --domain cortex.localhost        # HTTPS by its own local CA
    python3 scripts/cortex_hub.py --data DIR --host 0.0.0.0 --address 192.168.1.20 --domain cortex.lan   # the LAN

Every directory under the root with _brain/scripts/brain.py is a vault (CORTEX_VAULTS changes the root, as for the
life cycle). The views serve only what the panels need: each vault's Panel.html, its live feed
_brain/state/activity.js and the orbit.svg thumbnail. No note, config or usage log leaves the vault.

Routes: / the overview (one card per agent, or the wall: every panel embedded at once) · /api/vaults the figures the
overview polls · /v/<vault>/Panel.html, /v/<vault>/state/activity.js, /v/<vault>/generated/orbit.svg ·
POST /api/connect creates a vault (scripts/new_vault.py) or picks one, and returns the prompt that has the agent wire
itself · /ca.crt the local CA to trust · /login in the LAN scope.

Scope, chosen by the address browsers reach it at (--address, default --host): a loopback address is the localhost
scope; a private one is the LAN scope, which needs --data and signs browsers in with the access key in
DATA/access.key. Public addresses are refused. Requests naming any other host are refused too (DNS rebinding).
With --data the hub speaks HTTPS with a certificate from its own CA (scripts/hub_tls.py), limited to those names.
"""
import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ENGINE = Path(__file__).resolve().parents[1]   # the engine repository, when the hub runs from it
sys.path.insert(0, str(ENGINE / "installers"))
try:
    import wire_agent
except ImportError:   # a hub copied out of the repository: views only, no connect form
    wire_agent = None

ACTIVITY_PREFIX = "window.BRAIN_ACTIVITY = "
# the only files of a vault the hub serves, relative to its _brain folder
SERVED = {"Panel.html": "text/html; charset=utf-8", "state/activity.js": "text/javascript; charset=utf-8",
          "generated/orbit.svg": "image/svg+xml"}
LIVE_MINUTES = 10   # a vault with activity this recent shows as live
TEXT = "text/plain; charset=utf-8"
VAULT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}")
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
KINDS = ("claude-code", "codex", "gemini", "opencode", "hermes", "custom")
PYTHONS = ("python3", "python", "py")
SESSION_DAYS = 30


def vaults(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {p.name: p for p in sorted(root.iterdir()) if (p / "_brain/scripts/brain.py").is_file()}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def activity(v: Path) -> list[dict]:
    try:
        raw = (v / "_brain/state/activity.js").read_text(encoding="utf-8")
        return json.loads(raw[len(ACTIVITY_PREFIX):].rstrip().rstrip(";"))["events"]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def summary(name: str, v: Path) -> dict:
    """The figures of one vault's card: size by layer and status, and its latest activity."""
    g = read_json(v / "_brain/generated/graph.json", {})
    nodes = g.get("nodes", [])
    layers = {str(L): sum(n.get("layer") == L for n in nodes) for L in (1, 2, 3)}
    status = {s: sum(n.get("status") == s for n in nodes) for s in ("provisional", "seed", "consolidated", "obsolete")}
    events = activity(v)
    now = datetime.now()

    def age_min(e):
        try:
            return (now - datetime.fromisoformat(e["t"])).total_seconds() / 60
        except (KeyError, ValueError, TypeError):
            return None

    last = events[-1] if events else None
    day = [e for e in events if (age_min(e) or 1e9) < 1440]
    recent = []
    for e in reversed(events):
        if e.get("e") == "activated":
            continue
        recent.append({"t": e.get("t"), "e": e.get("e"), "what": e.get("q") or e.get("title") or e.get("note") or ""})
        if len(recent) == 4:
            break
    panel = v / "_brain/Panel.html"
    return {
        "name": name, "slug": quote(name), "version": g.get("version"), "nodes": len(nodes), "layers": layers,
        "status": status, "inbox": (g.get("cold") or {}).get("_inbox", 0),
        "built": datetime.fromtimestamp(panel.stat().st_mtime).isoformat(timespec="seconds") if panel.exists() else None,
        "seq": last.get("i", 0) if last else 0,
        "live": last is not None and (age_min(last) or 1e9) < LIVE_MINUTES,
        "last": last.get("t") if last else None,
        "today": {"recalls": sum(e.get("e") == "recall" for e in day), "reads": sum(e.get("e") == "read" for e in day),
                  "used": sum(e.get("e") == "used" for e in day)},
        "recent": recent,
    }


def subdomain(name: str) -> str:
    """The label of a vault's own host: Claude Cortex -> claude (claude.cortex.localhost)."""
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"\s+cortex$", "", name.strip(), flags=re.I).lower()).strip("-") or "cortex"


def host_path(base: str, *parts: str) -> str:
    """A path on the agents' machine, in its own style: C:\\Users\\me\\Vaults or /home/me/Vaults."""
    if re.match(r"[A-Za-z]:|\\\\", base) or ("\\" in base and "/" not in base):
        return "\\".join([base.replace("/", "\\").rstrip("\\"), *parts])
    return "/".join([base.rstrip("/"), *parts])


def agent_prompt(name: str, folder: str, kind: str, role: str, by: str, py: str, host_root: str, committed: bool) -> str:
    """What the user pastes to the agent: it checks the vault, writes its own memory block and tries one recall."""
    vault = host_path(host_root, folder)
    b = f'{py} "{host_path(vault, "_brain", "scripts", "brain.py")}"'
    block, skill = wire_agent.blocks(kind, name, b, by, role)
    marker, end = wire_agent.markers(name)
    target = wire_agent.targets(kind)
    target = f"`{str(target).replace(str(Path.home()), '~', 1)}` (your global instruction file)" if target else \
        "your own global instruction file, the one you load at the start of every session (if you have none, stop and ask me)"
    steps = [f"Check it: run `{b} doctor`. It needs Python 3.10+ and git on this machine; if either is missing or the path "
             "does not exist, stop and tell me."]
    if not committed:
        steps.append(f'The vault has no first commit yet: in `{vault}` run `git add -A` and '
                     f'`git commit -m "config: new vault from the Cortex template"` (the pre-commit hook validates it).')
    steps.append(f"Back up {target}, then put BLOCK A in it: if a block between `{marker}` and `{end}` is already there, "
                 "replace it, otherwise append it at the end. Change nothing else in the file.")
    if skill:
        steps.append(f"Write SKILL B to `~/.hermes/skills/{wire_agent.slug(name)}/SKILL.md` (your HERMES_HOME if it is set), "
                     "replacing any older copy.")
    steps.append(f'Run `{b} recall "how this vault works"` once to see it answer, then tell me in two lines what you '
                 "changed. From your next session on, follow the block.")
    text = (f"Connect yourself to your long-term memory: the Cortex vault \"{name}\" at `{vault}` on this machine.\n"
            f"You are its {role}.\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) +
            f"\n\n===== BLOCK A =====\n{block}\n===== END BLOCK A =====\n")
    if skill:
        text += f"\n===== SKILL B =====\n{skill.rstrip()}\n===== END SKILL B =====\n"
    return text


def load_scopes(data: Path | None) -> dict[str, str]:
    """Each vault's scope, kept by the hub in DATA/scopes.json: local (default) or lan."""
    return read_json(data / "scopes.json", {}) if data else {}


def save_scope(data: Path, name: str, scope: str) -> None:
    scopes = load_scopes(data)
    scopes[name] = scope
    tmp = data / "scopes.json.tmp"
    tmp.write_text(json.dumps(scopes, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(data / "scopes.json")


def connect(root: Path, host_root: str, form: dict, owner: str, lock: threading.Lock,
            data: Path | None = None, lan: bool = False) -> dict:
    """Validates the connect form, creates the vault when asked, and returns the agent's prompt. ValueError = bad input."""
    if wire_agent is None or not (ENGINE / "scripts/new_vault.py").is_file():
        raise ValueError("this hub runs outside the engine repository: connect agents with installers/wire_agent.py")
    get = lambda k, d="": str(form.get(k) or d).strip()
    label, kind, role, py, pick = get("agent"), get("kind"), get("role", "curator"), get("python", "python3"), get("vault")
    if not AGENT_NAME.fullmatch(label):
        raise ValueError("agent name: letters, digits, spaces, dots, dashes or underscores, up to 40")
    if kind not in KINDS or role not in ("curator", "reader") or py not in PYTHONS:
        raise ValueError("unknown agent, role or Python command")
    scope = get("scope", "local")
    if scope not in ("local", "lan") or (scope == "lan" and not lan):
        raise ValueError("scope: local, or lan when the hub has a LAN address")
    by = wire_agent.slug(label)
    log, created = "", False
    with lock:
        if pick:
            v = vaults(root).get(pick)
            if not v:
                raise ValueError(f"no vault named {pick!r}")
            folder = pick
        else:
            folder = get("name") or f"{label} Cortex"
            if not VAULT_NAME.fullmatch(folder) or ".." in folder:
                raise ValueError("cortex name: letters, digits, spaces, dots, dashes or underscores, up to 48")
            v = root / folder
            if v.exists():
                raise ValueError(f"{folder!r} already exists: pick it from the list instead")
            cmd = [sys.executable, str(ENGINE / "scripts/new_vault.py"), str(v), "--name", folder, "--agent", label,
                   "--owner", owner, "--learner", by]
            for opt, env in (("--git-name", "CORTEX_GIT_NAME"), ("--git-email", "CORTEX_GIT_EMAIL")):
                if os.environ.get(env):
                    cmd += [opt, os.environ[env]]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            log = (r.stdout + r.stderr).strip()
            if r.returncode:
                raise RuntimeError(f"new_vault.py failed:\n{log[-2000:]}")
            created = True
        if data:
            save_scope(data, folder, scope)
    cfg = read_json(v / "_brain/config.json", {})
    name = cfg.get("vault") or folder
    committed = subprocess.run(["git", "-C", str(v), "rev-parse", "-q", "--verify", "HEAD"], capture_output=True).returncode == 0
    prompt = agent_prompt(name, folder, kind, role, by, py, host_root, committed)
    return {"vault": name, "folder": folder, "scope": scope, "created": created, "log": log, "prompt": prompt}


class Server(ThreadingHTTPServer):
    """Speaks TLS when given a context; the handshake runs in the connection's own thread, never in accept."""
    daemon_threads = True
    ctx: ssl.SSLContext | None = None

    def finish_request(self, request, client_address):
        if self.ctx is None:
            return super().finish_request(request, client_address)
        request.settimeout(15)
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):   # a browser that does not trust the CA yet, a port scan
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            tls.close()


class Hub(BaseHTTPRequestHandler):
    root: Path = Path.home() / "Vaults"
    host_root: str | None = None   # the root as the agents' machine writes it (the container sees /vaults)
    allowed: set[str] = {"localhost", "127.0.0.1", "::1"}   # Host header names the hub answers to
    domain: str | None = None       # with a dot in it, each vault also answers at <agent>.<domain>
    public_port = 443               # the port browsers use, for the links between hosts
    key: str | None = None          # LAN scope: the access key browsers sign in with
    tls: Path | None = None         # directory with ca.crt, when the hub speaks HTTPS
    scope = "localhost"            # this listener's scope: localhost (every vault) or lan (the vaults marked lan)
    data: Path | None = None        # the hub's data folder: CA, access key, scopes.json
    lan_domain: str | None = None   # set when the hub also has a LAN listener: <machine>.cortex.home
    lan_ip: str | None = None       # the LAN listener's address, for the by-IP links (/v/<agent>/)
    owner = os.environ.get("CORTEX_OWNER", "the owner")
    csrf = secrets.token_urlsafe(24)
    sessions: dict[str, float] = {}
    creating = threading.Lock()
    server_version = "CortexHub/1"

    def log_message(self, fmt, *args):   # quiet: the panels poll every few seconds
        pass

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, where: str, headers: dict | None = None):
        self.send(HTTPStatus.FOUND, b"", TEXT, {"Location": where, **(headers or {})})

    def hostname(self) -> str:
        h = (self.headers.get("Host") or "").strip().lower()
        if h.startswith("["):
            return h[1:h.find("]")]
        return h.rsplit(":", 1)[0] if h.count(":") == 1 else h

    def signed_in(self) -> bool:
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return False
        tok = c.get("cortex_hub")
        return tok is not None and self.sessions.get(tok.value, 0) > time.time()

    def visible(self) -> dict[str, Path]:
        """The vaults this listener shows: all of them on localhost, only those marked lan on the LAN."""
        vs = vaults(self.root)
        if self.scope != "lan":
            return vs
        scopes = load_scopes(self.data)
        return {n: v for n, v in vs.items() if scopes.get(n) == "lan"}

    def vault_host(self) -> Path | None:
        """The vault whose own host this request names (claude.cortex.localhost), if any."""
        h, d = self.hostname(), self.domain
        if not d or "." not in d or not h.endswith("." + d):
            return None
        label = h[:-len(d) - 1]
        return next((v for n, v in self.visible().items() if subdomain(n) == label), None)

    def url(self, host: str) -> str:
        return f"https://{host}" + ("" if self.public_port == 443 else f":{self.public_port}") if self.tls else \
            f"http://{host}:{self.public_port}"

    def vault_url(self, name: str, scope: str) -> str | None:
        """Where a vault's panel lives for its scope: https://claude.cortex.local/ or https://claude.cortex.home/."""
        d = self.lan_domain if scope == "lan" else (self.domain if self.scope == "localhost" else None)
        return self.url(f"{subdomain(name)}.{d}") + "/" if d and "." in d else None

    def gate(self, path: str) -> bool:
        """Refuses foreign Host names and, in the LAN scope, anyone not signed in. True when it answered."""
        if self.hostname() not in self.allowed and self.vault_host() is None:
            self.send(HTTPStatus.MISDIRECTED_REQUEST, b"this hub does not answer to that host name", TEXT)
            return True
        if self.key and path not in ("/login", "/ca.crt") and not self.signed_in():
            if path.startswith("/api/") or self.command == "POST":
                self.send(HTTPStatus.UNAUTHORIZED, b"sign in first", TEXT)
            else:
                self.redirect("/login")
            return True
        return False

    def body(self, limit: int = 8192) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request too large")
        return self.rfile.read(n)

    def same_origin(self) -> bool:
        o = self.headers.get("Origin")
        return o is not None and (urlsplit(o).hostname or "") in self.allowed

    def do_POST(self):
        path = urlsplit(self.path).path
        if self.gate(path):
            return
        if not self.same_origin():
            return self.send(HTTPStatus.FORBIDDEN, b"cross-site request refused", TEXT)
        try:
            raw = self.body()
        except ValueError:
            return self.send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"request too large", TEXT)
        if path == "/login":
            key = (parse_qs(raw.decode("utf-8", "replace")).get("key") or [""])[0].strip()
            if self.key and hmac.compare_digest(key.encode(), self.key.encode()):
                tok = secrets.token_urlsafe(32)
                self.sessions[tok] = time.time() + SESSION_DAYS * 86400
                shared = f"Domain={self.domain}; " if self.domain and "." in self.domain else ""   # the agents' hosts too
                return self.redirect("/", {"Set-Cookie": f"cortex_hub={tok}; {shared}Path=/; HttpOnly; Secure; "
                                                         f"SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"})
            time.sleep(1)   # slows guessing
            return self.send(HTTPStatus.UNAUTHORIZED, LOGIN.replace("<!--err-->", "<p class=err>Wrong key.</p>").encode(),
                             "text/html; charset=utf-8")
        if path == "/api/connect":
            if self.scope != "localhost":   # vaults live on this machine: only its owner connects agents
                return self.send(HTTPStatus.FORBIDDEN, b"connect agents from the hub on this machine", TEXT)
            if not hmac.compare_digest(self.headers.get("X-Cortex-Csrf") or "", self.csrf):
                return self.send(HTTPStatus.FORBIDDEN, b"stale page: reload it", TEXT)
            try:
                out = connect(self.root, self.host_root or str(self.root), json.loads(raw or b"{}"), self.owner, self.creating,
                              self.data, self.lan_domain is not None)
                out["url"] = self.vault_url(out["folder"], out["scope"])
            except ValueError as e:
                return self.send(HTTPStatus.BAD_REQUEST, json.dumps({"error": str(e)}).encode(), "application/json")
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                return self.send(HTTPStatus.INTERNAL_SERVER_ERROR, json.dumps({"error": str(e)}).encode(), "application/json")
            return self.send(200, json.dumps(out).encode(), "application/json")
        self.send(404, b"not found", TEXT)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.gate(path):
            return
        own = self.vault_host()
        if own is not None:   # an agent's own host: its panel at the root
            rel = "Panel.html" if path in ("/", "/Panel.html") else path.lstrip("/")
            if rel in SERVED:
                f = own / "_brain" / rel
                if f.is_file():
                    return self.send(200, f.read_bytes(), SERVED[rel])
                if rel == "state/activity.js":
                    return self.send(200, b"", SERVED[rel])
            if path == "/login" and self.key:
                return self.redirect(self.url(self.domain) + "/login")
            return self.send(404, b"not found", TEXT)
        if path == "/login":
            return self.send(200, LOGIN.encode(), "text/html; charset=utf-8") if self.key else self.redirect("/")
        if path == "/ca.crt" and self.tls:
            return self.send(200, (self.tls / "ca.crt").read_bytes(), "application/x-x509-ca-cert",
                             {"Content-Disposition": 'attachment; filename="cortex-hub-ca.crt"'})
        if path == "/":
            return self.send(200, PAGE.replace("__CSRF__", self.csrf).encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/vaults":
            scopes = load_scopes(self.data)
            data = [summary(n, v) for n, v in self.visible().items()]
            for d in data:
                d["scope"] = scopes.get(d["name"], "local")
                if self.domain and "." in self.domain:
                    d["url"] = self.url(f"{subdomain(d['name'])}.{self.domain}") + "/"
                if d["scope"] == "lan" and self.lan_domain:
                    d["lan_url"] = self.vault_url(d["name"], "lan")
                if self.lan_ip and d["scope"] == "lan":
                    d["ip_url"] = self.url(self.lan_ip) + f"/v/{subdomain(d['name'])}/"
            hub = {"scope": self.scope, "tls": self.tls is not None, "login": self.key is not None, "domain": self.domain,
                   "lan": self.lan_domain if self.scope == "localhost" else None,
                   "connect": self.scope == "localhost" and wire_agent is not None and (ENGINE / "scripts/new_vault.py").is_file()}
            return self.send(200, json.dumps({"vaults": data, "hub": hub, "t": datetime.now().isoformat(timespec="seconds")}).encode(),
                             "application/json")
        if re.fullmatch(r"/v/[^/]+", path):
            return self.redirect(path + "/")
        m = re.fullmatch(r"/v/([^/]+)/(.*)", path)
        if m:   # /v/<vault name or agent label>/: every panel by address alone, for a network that does not resolve names
            name, rel = unquote(m.group(1)), m.group(2) or "Panel.html"
            shown = self.visible()
            v = shown.get(name) or next((v for n, v in shown.items() if subdomain(n) == name), None)
            if v and rel in SERVED:
                f = v / "_brain" / rel
                if f.is_file():
                    return self.send(200, f.read_bytes(), SERVED[rel])
                if rel == "state/activity.js":   # no activity yet: an empty feed, not an error the panel would report
                    return self.send(200, b"", SERVED[rel])
        self.send(404, b"not found", "text/plain; charset=utf-8")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cortex hub</title>
<meta name="cortex-csrf" content="__CSRF__">
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
  --border: rgba(11,11,11,0.10); --wash: rgba(11,11,11,0.04);
  --l1: #2a78d6; --l2: #eb6834; --l3: #1baf7a; --good: #0ca30c; --warning: #fab219;
  --shadow: 0 6px 24px rgba(11,11,11,0.12);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
    --border: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.05);
    --l1: #3987e5; --l2: #d95926; --l3: #199e70; --shadow: 0 6px 24px rgba(0,0,0,0.5);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink2: #c3c2b7; --muted: #898781;
  --border: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.05);
  --l1: #3987e5; --l2: #d95926; --l3: #199e70; --shadow: 0 6px 24px rgba(0,0,0,0.5);
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--page); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1600px; margin: 0 auto; padding: 16px; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 14px; }
h1 { font-size: 20px; font-weight: 650; margin: 0; }
.sub { color: var(--ink2); font-size: 12px; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; }
button, a.btn { font: inherit; font-size: 12px; border: 1px solid var(--border); background: var(--surface); color: var(--ink);
  border-radius: 999px; padding: 4px 10px; cursor: pointer; text-decoration: none; display: inline-block; }
button:hover, a.btn:hover { background: var(--wash); }
button[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; min-width: 0;
  display: flex; flex-direction: column; gap: 8px; transition: box-shadow .4s, border-color .4s; }
.card.ping { border-color: var(--ink); box-shadow: 0 0 0 3px var(--wash); }
.card h2 { font-size: 15px; margin: 0; display: flex; align-items: center; gap: 8px; }
.card h2 .v { margin-left: auto; font-size: 11px; font-weight: 500; color: var(--muted); }
.pulse { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
.live .pulse { background: var(--good); animation: pulse 2s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .3; } }
@media (prefers-reduced-motion: reduce) { .live .pulse { animation: none; } }
.thumb { display: block; aspect-ratio: 1; width: 100%; max-height: 260px; object-fit: contain; border-radius: 8px; background: var(--page); }
.stats { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--ink2); }
.stats b { color: var(--ink); font-weight: 650; font-variant-numeric: tabular-nums; }
.key { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.feed { list-style: none; margin: 0; padding: 0; font-size: 12px; color: var(--ink2); }
.feed li { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 2px 0; border-top: 1px solid var(--border); }
.feed li:first-child { border-top: 0; color: var(--ink); }
.feed .t { color: var(--muted); font-variant-numeric: tabular-nums; margin-right: 6px; }
.actions { display: flex; gap: 6px; margin-top: auto; align-items: center; }
.host { margin-left: auto; font-size: 11px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { color: var(--muted); font-size: 13px; }
/* the wall: every agent's panel at once, orbit only */
.wall { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr)); gap: 12px; }
.frame { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
.frame .bar { display: flex; align-items: center; gap: 8px; padding: 6px 10px; font-size: 13px; font-weight: 600; }
.frame .bar a { margin-left: auto; }
.frame iframe { border: 0; width: 100%; height: min(70vh, 640px); display: block; background: var(--page); }
.foot { margin-top: 18px; color: var(--muted); font-size: 12px; }
.foot a { color: var(--ink2); }
button.primary { background: var(--ink); color: var(--surface); border-color: var(--ink); }
dialog { border: 1px solid var(--border); border-radius: 14px; background: var(--surface); color: var(--ink); padding: 18px;
  width: min(640px, calc(100vw - 32px)); box-shadow: var(--shadow); }
dialog::backdrop { background: rgba(0,0,0,0.35); }
dialog h2 { font-size: 16px; margin: 0 0 4px; }
.form { display: grid; gap: 10px; margin-top: 12px; }
.form label { display: grid; gap: 4px; font-size: 12px; color: var(--ink2); }
.form input, .form select, dialog textarea { font: inherit; font-size: 13px; color: var(--ink); background: var(--page);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 8px; width: 100%; }
dialog textarea { font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; height: min(46vh, 420px); resize: vertical; margin-top: 10px; }
dialog pre { font-size: 11px; white-space: pre-wrap; max-height: 160px; overflow: auto; color: var(--ink2); }
.row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; flex-wrap: wrap; }
.err { color: #d03b3b; font-size: 12px; margin: 0; min-height: 1em; }
[hidden] { display: none !important; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>Cortex hub</h1><div class="sub" id="sub">Loading…</div></div>
    <div class="controls">
      <button type="button" id="m-grid" aria-pressed="true">Agents</button>
      <button type="button" id="m-wall" aria-pressed="false">Wall</button>
      <button type="button" id="theme">Theme</button>
      <button type="button" id="connect-open" class="primary" hidden>Connect an agent</button>
    </div>
  </header>
  <main>
    <section class="grid" id="grid" aria-label="One card per agent"></section>
    <section class="wall" id="wall" aria-label="Every agent's panel" hidden></section>
  </main>
  <p class="foot" id="foot"></p>
</div>
<dialog id="connect" aria-labelledby="c-title">
  <form id="c-form" class="form-wrap">
    <h2 id="c-title">Connect an agent</h2>
    <div class="sub">The hub prepares the cortex; the agent wires itself from the prompt you paste to it.</div>
    <div class="form">
      <label>Agent name <input name="agent" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9 ._\-]*" placeholder="Codex" autocomplete="off"></label>
      <label>Agent
        <select name="kind">
          <option value="claude-code">Claude Code</option><option value="codex">Codex CLI</option>
          <option value="gemini">Gemini CLI</option><option value="opencode">OpenCode</option>
          <option value="hermes">Hermes Agent</option><option value="custom">Another agent (it names its own instruction file)</option>
        </select></label>
      <label>Cortex <select name="vault" id="c-vault"><option value="">A new cortex for this agent</option></select></label>
      <label id="c-scope-row">Domain scope
        <select name="scope" id="c-scope">
          <option value="local">Local: only this machine</option>
          <option value="lan">LAN: the local network, behind the access key</option>
        </select><span class="sub" id="c-host"></span></label>
      <label id="c-name-row">Name of the new cortex <input name="name" maxlength="48" pattern="[A-Za-z0-9][A-Za-z0-9 ._\-]*" placeholder="Codex Cortex" autocomplete="off"></label>
      <label>Role
        <select name="role">
          <option value="curator">Curator: reads, learns and tends the cortex</option>
          <option value="reader">Reader: reads and learns; another agent tends it</option>
        </select></label>
      <label>Python on the agent's machine
        <select name="python"><option>python3</option><option>python</option><option value="py">py (Windows launcher)</option></select></label>
    </div>
    <p class="err" id="c-err" role="alert"></p>
    <div class="row"><button type="button" id="c-cancel">Cancel</button><button type="submit" class="primary" id="c-go">Create the prompt</button></div>
  </form>
  <div id="c-out" hidden>
    <h2>Paste this to <span id="c-who"></span></h2>
    <div class="sub" id="c-what"></div>
    <textarea id="c-text" readonly aria-label="Prompt for the agent"></textarea>
    <details id="c-logbox"><summary class="sub">Creation log</summary><pre id="c-log"></pre></details>
    <div class="row"><button type="button" id="c-copy" class="primary">Copy</button><button type="button" id="c-done">Done</button></div>
  </div>
</dialog>
<script>
(() => {
  const $ = s => document.querySelector(s);
  const el = (tag, attrs = {}, text, parent) => {
    const e = document.createElement(tag);
    for (const k in attrs) k === 'class' ? (e.className = attrs[k]) : e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const store = {
    get(k, d) { try { const v = localStorage.getItem('cortex-hub:' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('cortex-hub:' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const root = document.documentElement;
  const theme = () => root.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  if (store.get('theme', null)) root.dataset.theme = store.get('theme');
  $('#theme').addEventListener('click', () => { root.dataset.theme = theme() === 'dark' ? 'light' : 'dark'; store.set('theme', root.dataset.theme); if (mode === 'wall') renderWall(true); });
  const hhmm = t => String(t || '').slice(11, 16);
  const ago = t => {
    if (!t) return 'no activity yet';
    const m = Math.round((Date.now() - Date.parse(t)) / 60000);
    return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
  };
  const panelUrl = (v, extra = '') => `/v/${v.slug}/Panel.html${extra}`;   // same origin: the wall embeds these
  const openUrl = (v, extra = '') => v.url ? v.url + extra : panelUrl(v, extra);   // the agent's own host
  let hubInfo = {}, vaults = [], mode = store.get('mode', 'grid');
  const seq = {}, cards = {};

  function card(v) {
    const c = el('article', { class: 'card' });
    const h = el('h2', {}, null, c);
    el('span', { class: 'pulse', 'aria-hidden': 'true' }, null, h);
    el('span', { class: 'n' }, v.name.replace(/ Cortex$/, ''), h);
    el('span', { class: 'v' }, null, h);
    const a = el('a', { href: openUrl(v), target: '_blank', rel: 'noopener', 'aria-label': `Open the ${v.name} panel` }, null, c);
    el('img', { class: 'thumb', alt: `Orbit of ${v.name}`, loading: 'lazy', src: `/v/${v.slug}/generated/orbit.svg` }, null, a);
    el('div', { class: 'stats' }, null, c);
    el('ul', { class: 'feed', 'aria-label': 'Latest activity' }, null, c);
    const act = el('div', { class: 'actions' }, null, c);
    el('a', { class: 'btn', href: openUrl(v), target: '_blank', rel: 'noopener' }, 'Open panel', act);
    el('a', { class: 'btn', href: openUrl(v, '#3d'), target: '_blank', rel: 'noopener' }, '3D', act);

    return c;
  }
  function fill(c, v) {
    c.classList.toggle('live', !!v.live);
    const hs = c.querySelector('.host') || el('span', { class: 'host' }, null, c.querySelector('.actions'));
    hs.textContent = v.lan_url ? `LAN · ${new URL(v.lan_url).host}` : v.url ? new URL(v.url).host : '';
    hs.title = v.lan_url ? 'Reachable from the local network, behind the access key' : 'Only on this machine';
    let ip = c.querySelector('.by-ip');
    if (v.ip_url && !ip) ip = el('a', { class: 'btn by-ip', target: '_blank', rel: 'noopener', title: 'When the network does not resolve the name' }, 'By IP', c.querySelector('.actions'));
    if (ip) ip.href = v.ip_url;
    c.querySelector('.v').textContent = v.version ? `engine ${v.version}` : '';
    const s = c.querySelector('.stats'); s.replaceChildren();
    const stat = (label, val, color) => { const d = el('span', {}, null, s); if (color) el('span', { class: 'key', style: `background:var(${color})` }, null, d); el('b', {}, String(val), d); d.append(' ' + label); };
    stat('nodes', v.nodes);
    stat('L1', v.layers['1'], '--l1'); stat('L2', v.layers['2'], '--l2'); stat('L3', v.layers['3'], '--l3');
    stat('seeds', v.status.seed + v.status.provisional);
    if (v.inbox) stat('in inbox', v.inbox);
    stat('recalls today', v.today.recalls);
    const f = c.querySelector('.feed'); f.replaceChildren();
    if (!v.recent.length) el('li', { class: 'empty' }, 'No agent activity yet', f);
    for (const e of v.recent) { const li = el('li', {}, null, f); el('span', { class: 't' }, hhmm(e.t), li); li.append(`${e.e} ${e.what}`); }
    c.title = `Last activity ${ago(v.last)} · panel built ${String(v.built || '').replace('T', ' ')}`;
    if (seq[v.name] != null && v.seq > seq[v.name]) { c.classList.add('ping'); setTimeout(() => c.classList.remove('ping'), 1500); }
    seq[v.name] = v.seq;
  }
  function renderGrid() {
    const g = $('#grid');
    for (const v of vaults) {
      if (!cards[v.name]) { cards[v.name] = card(v); g.appendChild(cards[v.name]); }
      fill(cards[v.name], v);
    }
    if (!vaults.length && !g.children.length) el('p', { class: 'empty' }, 'No vaults found under the hub root.', g);
  }
  let wallTheme = null;
  function renderWall(force) {
    const w = $('#wall');
    if (!force && wallTheme === theme() && w.children.length === vaults.length) return;
    wallTheme = theme(); w.replaceChildren();
    for (const v of vaults) {
      const f = el('section', { class: 'frame' }, null, w);
      const bar = el('div', { class: 'bar' }, null, f);
      el('span', {}, v.name.replace(/ Cortex$/, ''), bar);
      el('a', { class: 'btn', href: openUrl(v), target: '_blank', rel: 'noopener' }, 'Open', bar);
      el('iframe', { title: `${v.name} panel`, loading: 'lazy', src: panelUrl(v, `?embed&theme=${wallTheme}`) }, null, f);
    }
  }
  function setMode(m) {
    mode = m; store.set('mode', m);
    $('#grid').hidden = m !== 'grid'; $('#wall').hidden = m !== 'wall';
    $('#m-grid').setAttribute('aria-pressed', String(m === 'grid')); $('#m-wall').setAttribute('aria-pressed', String(m === 'wall'));
    if (m === 'wall') renderWall(); else $('#wall').replaceChildren();   // a hidden wall would keep seven panels animating
    wallTheme = m === 'wall' ? wallTheme : null;
  }
  $('#m-grid').addEventListener('click', () => setMode('grid'));
  $('#m-wall').addEventListener('click', () => setMode('wall'));
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch('/api/vaults', { cache: 'no-store' });
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json(); vaults = j.vaults; hubInfo = j.hub;
      $('#connect-open').hidden = !j.hub.connect;
      $('#foot').replaceChildren(`${j.hub.scope === 'lan' ? 'Local network: the cortexes marked LAN' : 'This machine: every cortex'} · ${j.hub.tls ? 'HTTPS by the hub\'s own CA · ' : 'plain HTTP'}`);
      if (j.hub.tls) el('a', { href: '/ca.crt' }, 'download ca.crt to trust it', $('#foot'));
      const live = vaults.filter(v => v.live).length;
      $('#sub').textContent = `${vaults.length} agents · ${live} active in the last 10 minutes · ${vaults.reduce((s, v) => s + v.nodes, 0)} nodes`;
      renderGrid();
      if (mode === 'wall') renderWall();
    } catch (e) { $('#sub').textContent = 'The hub is not answering'; }
  }
  const csrf = document.querySelector('meta[name="cortex-csrf"]').content;
  const dlg = $('#connect'), cform = $('#c-form');
  function openConnect() {
    const sel = $('#c-vault'), keep = sel.value;
    sel.replaceChildren(el('option', { value: '' }, 'A new cortex for this agent'));
    for (const v of vaults) el('option', { value: v.name }, `${v.name} (shared)`, sel);
    sel.value = keep; sync();
    $('#c-err').textContent = ''; cform.hidden = false; $('#c-out').hidden = true;
    dlg.showModal(); cform.agent.focus();
  }
  function sync() {
    const isNew = !cform.vault.value;
    $('#c-name-row').hidden = !isNew;
    const short = (cform.vault.value || cform.name.value.trim() || `${cform.agent.value.trim() || 'Codex'} Cortex`)
      .replace(/\s+cortex$/i, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'cortex';
    $('#c-scope-row').hidden = !hubInfo.lan;
    if (!hubInfo.lan) cform.scope.value = 'local';
    if (!isNew && !cform.dataset.touched) cform.scope.value = (vaults.find(v => v.name === cform.vault.value) || {}).scope || 'local';
    $('#c-host').textContent = `${short}.${cform.scope.value === 'lan' ? hubInfo.lan : hubInfo.domain}`;
    cform.name.placeholder = `${cform.agent.value.trim() || 'Codex'} Cortex`;
    if (!isNew && cform.role.value === 'curator') cform.role.value = 'reader';
  }
  cform.addEventListener('input', ev => { if (ev.target.name === 'scope') cform.dataset.touched = '1'; if (ev.target.name === 'vault') delete cform.dataset.touched; sync(); });
  cform.addEventListener('submit', async ev => {
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(cform));
    $('#c-go').disabled = true; $('#c-err').textContent = body.vault ? 'Preparing…' : 'Creating the cortex…';
    try {
      const r = await fetch('/api/connect', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Cortex-Csrf': csrf }, body: JSON.stringify(body) });
      const out = await r.json().catch(() => ({ error: `The hub answered ${r.status}` }));
      if (!r.ok) throw new Error(out.error || r.status);
      $('#c-who').textContent = body.agent;
      $('#c-what').textContent = out.created ? `New cortex "${out.vault}" created. The agent checks it, adds its memory block and tries one recall.`
                                             : `It joins "${out.vault}". The agent checks it, adds its memory block and tries one recall.`;
      if (out.url) { const a = el('a', { href: out.url, target: '_blank', rel: 'noopener' }, new URL(out.url).host); $('#c-what').append(' Panel: ', a); }
      $('#c-text').value = out.prompt; $('#c-log').textContent = out.log; $('#c-logbox').hidden = !out.log;
      cform.hidden = true; $('#c-out').hidden = false; $('#c-copy').textContent = 'Copy';
      poll();
    } catch (e) { $('#c-err').textContent = String(e.message || e); }
    finally { $('#c-go').disabled = false; }
  });
  $('#c-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#c-text').value); $('#c-copy').textContent = 'Copied'; }
    catch (e) { $('#c-text').select(); $('#c-copy').textContent = 'Press Ctrl+C'; }
  });
  $('#c-cancel').addEventListener('click', () => dlg.close());
  $('#c-done').addEventListener('click', () => { dlg.close(); cform.reset(); delete cform.dataset.touched; });
  $('#connect-open').addEventListener('click', openConnect);
  setMode(mode);
  poll(); setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
</script>
</body>
</html>
"""

class ToHttps(Hub):
    """Plain HTTP: sends browsers that typed the bare name to the same host over HTTPS, nothing else."""

    def do_GET(self):
        if self.hostname() not in self.allowed and self.vault_host() is None:
            return self.send(HTTPStatus.MISDIRECTED_REQUEST, b"this hub does not answer to that host name", TEXT)
        host = self.hostname()
        if urlsplit(self.path).path == "/ca.crt":   # public: a phone fetches it here before it trusts anything
            return self.send(200, (self.tls / "ca.crt").read_bytes(), "application/x-x509-ca-cert",
                             {"Content-Disposition": 'attachment; filename="cortex-hub-ca.crt"'})
        self.send(HTTPStatus.MOVED_PERMANENTLY, b"", TEXT, {"Location": self.url(f"[{host}]" if ":" in host else host) + self.path})

    def do_POST(self):
        self.send(HTTPStatus.FORBIDDEN, b"use HTTPS", TEXT)


LOGIN = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cortex hub</title>
<style>
:root { color-scheme: light dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText;
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
form { display: grid; gap: 10px; width: min(360px, calc(100vw - 32px)); }
h1 { font-size: 20px; margin: 0; }
p { margin: 0; color: GrayText; font-size: 12px; }
input, button { font: inherit; padding: 7px 10px; border-radius: 8px; border: 1px solid GrayText; }
button { cursor: pointer; }
.err { color: #d03b3b; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>Cortex hub</h1>
  <p>Local network scope. Sign in with the access key in the hub's data folder (access.key).</p>
  <!--err-->
  <input type="password" name="key" aria-label="Access key" autocomplete="current-password" required autofocus>
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""


def listen(cls: type, host: str, port: int, ctx: ssl.SSLContext | None, redirect: int | None) -> Server:
    httpd = Server((host, port), cls)
    httpd.ctx = ctx
    if redirect and ctx:
        plain = ThreadingHTTPServer((host, redirect), type("ToHttps", (ToHttps, cls), {}))
        threading.Thread(target=plain.serve_forever, daemon=True).start()
    return httpd


def main() -> None:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=env("CORTEX_VAULTS", str(Path.home() / "Vaults")))
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--address", help="address browsers reach the hub at, when it differs from --host (a container)")
    ap.add_argument("--domain", default="localhost", help="host name browsers use; the certificate covers it")
    ap.add_argument("--data", help="folder for the local CA and the access key; turns HTTPS on")
    ap.add_argument("--host-root", help="the vault root as the agents' machine writes it, for the prompts")
    ap.add_argument("--public-port", type=int, help="port browsers use, when it differs from --port (443: none in the links)")
    ap.add_argument("--redirect-port", type=int, help="also listen here in plain HTTP and send browsers to HTTPS")
    ap.add_argument("--dns-port", type=int, help="also answer DNS here (UDP) for the domain and its subdomains")
    lan = ap.add_argument_group("a second listener for the local network: the vaults marked lan, behind the access key")
    lan.add_argument("--lan-address", default=env("CORTEX_LAN_ADDRESS") or None, help="this machine's private LAN address")
    lan.add_argument("--lan-host", default=env("CORTEX_LAN_HOST") or None,
                     help="this machine's name: the LAN domain is <name>.<--lan-domain> (alastor.cortex.home)")
    lan.add_argument("--lan-domain", default=env("CORTEX_LAN_DOMAIN") or "cortex.home")
    lan.add_argument("--lan-port", type=int, default=9443)
    lan.add_argument("--lan-redirect-port", type=int)
    lan.add_argument("--lan-dns-port", type=int)
    a = ap.parse_args()
    import hub_tls
    address = a.address or a.host
    try:
        scope = hub_tls.scope(address)
        dns, ips = hub_tls.names(a.domain, address)
        if a.lan_address:
            if hub_tls.scope(a.lan_address) != "lan":
                raise ValueError(f"--lan-address {a.lan_address} is not a private LAN address")
            if not a.lan_host or not hub_tls.HOSTNAME.fullmatch(a.lan_host.lower()) or "." in a.lan_host:
                raise ValueError("--lan-host: this machine's name, one label (alastor)")
            lan_dns, lan_ips = hub_tls.names(f"{a.lan_host.lower()}.{a.lan_domain}", a.lan_address)
    except ValueError as e:
        sys.exit(str(e))
    if (scope == "lan" or a.lan_address) and not a.data:
        sys.exit("the LAN scope needs --data: HTTPS and an access key")
    Hub.root, Hub.data = Path(a.root).expanduser(), Path(a.data).expanduser() if a.data else None
    Hub.host_root = a.host_root or str(Hub.root)
    Hub.public_port = a.public_port or a.port
    Hub.lan_domain = lan_dns[0] if a.lan_address else None
    Hub.lan_ip = a.lan_address
    ctx, scheme = None, "http"
    if Hub.data:
        names = dns + (lan_dns if a.lan_address else [])
        crt, crt_key, done = hub_tls.ensure(Hub.data / "tls", list(dict.fromkeys(names)),
                                        list(dict.fromkeys(ips + (lan_ips if a.lan_address else []))))
        for line in done:
            print(line, flush=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(crt, crt_key)
        Hub.tls, scheme = Hub.data / "tls", "https"
        key = None
        if scope == "lan" or a.lan_address:
            kf = Hub.data / "access.key"
            if not kf.is_file():
                old = os.umask(0o077)
                kf.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
                os.umask(old)
                print(f"new access key in {kf}: read it on this machine to sign in", flush=True)
            key = kf.read_text(encoding="utf-8").strip()
    own = lambda names: {d for d in names if not d.startswith("*.")}
    main_hub = type("MainHub", (Hub,), {"scope": scope, "domain": dns[0], "allowed": own(dns) | set(ips),
                                        "key": key if scope == "lan" else None})
    httpd = listen(main_hub, a.host, a.port, ctx, a.redirect_port)
    print(f"cortex hub, {scope} scope, {scheme} for {', '.join(dns + ips)} · listening on {a.host}:{a.port} · "
          f"{len(vaults(Hub.root))} vaults under {Hub.root}", flush=True)
    if a.dns_port:
        import hub_dns
        zones = {dns[0]: address, **({lan_dns[0]: a.lan_address} if a.lan_address else {})}   # this machine knows both
        hub_dns.serve(a.host, a.dns_port, zones)
        print(f"DNS on {a.host}:{a.dns_port}/udp: " + ", ".join(f"*.{d} = {ip}" for d, ip in zones.items()), flush=True)
    if a.lan_address:
        lan_hub = type("LanHub", (Hub,), {"scope": "lan", "domain": lan_dns[0], "key": key,
                                          "allowed": own(lan_dns) | set(lan_ips)})
        lan_httpd = listen(lan_hub, a.host, a.lan_port, ctx, a.lan_redirect_port)
        threading.Thread(target=lan_httpd.serve_forever, daemon=True).start()
        print(f"LAN listener for {', '.join(lan_dns + lan_ips)} on {a.host}:{a.lan_port}: the vaults marked lan, "
              "behind the access key", flush=True)
        if a.lan_dns_port:
            import hub_dns
            hub_dns.serve(a.host, a.lan_dns_port, {lan_dns[0]: a.lan_address})
            print(f"DNS on {a.host}:{a.lan_dns_port}/udp: {lan_dns[0]} and *.{lan_dns[0]} are {a.lan_address}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
