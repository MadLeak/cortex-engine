#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Cortex hub: one web page with every agent's brain panel, live.

    python3 scripts/cortex_hub.py [--root ~/Vaults] [--host 127.0.0.1] [--port 8790]
    python3 scripts/cortex_hub.py --data ~/.cortex-hub --domain cortex.localhost        # HTTPS by its own local CA
    python3 scripts/cortex_hub.py --data DIR --host 0.0.0.0 --address 192.168.1.20 --domain cortex.lan   # the LAN

Every directory under the root with _brain/scripts/brain.py is a vault (CORTEX_VAULTS changes the root, as for the
life cycle). The views serve only what the panels need: each vault's Panel.html, its live feed
_brain/state/activity.js and the orbit.svg thumbnail. No note, config or usage log leaves the vault.

Routes: the hub is a single-page app: /, /agents (one card per agent), /wall (every panel embedded at once), /review
(the local model's verdicts on the seeds, this machine only), /connect (the connect dialog over Agents) and /panel/<slug>
(one cortex's panel) all answer the same page, and its navigation bar switches the view in place with the History API ·
/api/vaults the figures the page polls · /v/<vault>/Panel.html, /v/<vault>/state/activity.js, /v/<vault>/generated/orbit.svg ·
POST /api/connect creates a vault (scripts/new_vault.py) or picks one, and returns the prompt that has the agent wire
itself · /api/review the verdicts (scripts/review_prep.py), POST /api/review/apply the owner's decisions
(scripts/review_apply.py), both on this machine only · /ca.crt the local CA to trust · /login in the LAN scope.

Scope, chosen by the address browsers reach it at (--address, default --host): a loopback address is the localhost
scope; a private one is the LAN scope, which needs --data and signs browsers in with the access key in
DATA/access.key. Public addresses are refused. Requests naming any other host are refused too (DNS rebinding).
With --data the hub speaks HTTPS with a certificate from its own CA (scripts/hub_tls.py), limited to those names;
--local-http keeps this machine's listener in plain HTTP (nothing to trust on the host) and HTTPS for the LAN only.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cortex_mcp  # noqa: E402  (the MCP service rides the hub's own port: /mcp/<vault>)

ACTIVITY_PREFIX = "window.BRAIN_ACTIVITY = "
# the only files of a vault the hub serves, relative to its _brain folder
SERVED = {"Panel.html": "text/html; charset=utf-8", "state/activity.js": "text/javascript; charset=utf-8",
          "generated/orbit.svg": "image/svg+xml"}
LIVE_MINUTES = 10   # a vault with activity this recent shows as live
TEXT = "text/plain; charset=utf-8"
VIEWS = ("/", "/agents", "/wall", "/review", "/connect")
VAULT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}")
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
KINDS = ("claude-code", "codex", "gemini", "opencode", "hermes", "custom")
PYTHONS = ("python3", "python", "py")
SESSION_DAYS = 30
CREDIT = ('Cortex &copy; 2026 Adhonys Diaz &middot; <span class="by">By MadLeak</span> &middot; '
          '<a href="https://polyformproject.org/licenses/noncommercial/1.0.0" target="_blank" rel="noopener noreferrer">PolyForm Noncommercial 1.0.0</a>')


def vaults(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {p.name: p for p in sorted(root.iterdir()) if (p / "_brain/scripts/brain.py").is_file()}


HUB_IDENTITY = {"GIT_AUTHOR_NAME": "Cortex hub", "GIT_AUTHOR_EMAIL": "hub@cortex.local",
                "GIT_COMMITTER_NAME": "Cortex hub", "GIT_COMMITTER_EMAIL": "hub@cortex.local"}


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def upgrade_engines(root: Path, want: str) -> list[str]:
    """Every vault on the hub's own engine: an older one gets this engine (scripts/upgrade_engine.py) and, when the
    vault has commits and nothing else staged, one commit `engine: v<version>` by the hub. A newer one is left alone,
    and the resident engine does not serve it (it runs its own brain.py)."""
    out = []
    for name, v in vaults(root).items():
        try:
            m = cortex_mcp.VERSION_LINE.search((v / "_brain/scripts/brain_core.py").read_text(encoding="utf-8"))
        except OSError:
            continue
        have = m.group(1) if m else "?"
        if version_tuple(have) >= version_tuple(want):
            if version_tuple(have) > version_tuple(want):
                out.append(f"{name}: engine v{have} is newer than the hub's v{want}: left alone")
            continue
        r = subprocess.run([sys.executable, str(ENGINE / "scripts/upgrade_engine.py"), str(v)],
                           capture_output=True, text=True, timeout=300)
        if "update" not in r.stdout:
            out.append(f"{name}: upgrade from v{have} failed: {(r.stderr or r.stdout).strip()[-300:]}")
            continue
        g = lambda *a: cortex_mcp.git(v, *a, env={**os.environ, **HUB_IDENTITY})   # noqa: E731
        if g("rev-parse", "-q", "--verify", "HEAD").returncode != 0:
            out.append(f"{name}: engine v{have} -> v{want} (no first commit yet: left for its owner to commit)")
            continue
        if g("diff", "--cached", "--name-only").stdout.strip():
            out.append(f"{name}: engine v{have} -> v{want} (something else is staged: left uncommitted)")
            continue
        g("add", "--", "_brain/scripts", ".githooks")
        c = g("commit", "-q", "-m", f"engine: v{want}", "-m", "upgraded by the Cortex hub on start")
        out.append(f"{name}: engine v{have} -> v{want}" + ("" if c.returncode == 0 else
                                                         f" (the commit was refused: {(c.stderr or c.stdout).strip()[-200:]})"))
    return out


def migrate_vaults(root: Path) -> list[str]:
    """Every vault's data up to its engine's schema (engine 2.17+): `brain.py migrate --commit`, one commit per
    migration by the Cortex hub. Deterministic code, no model; a migration that cannot run waits and says why."""
    out = []
    for name, v in vaults(root).items():
        if not cortex_mcp.engine_at_least(v, (2, 17, 0)):
            continue
        r = subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "--vault", str(v), "migrate", "--commit"],
                           capture_output=True, text=True, timeout=600, env={**os.environ, **HUB_IDENTITY})
        lines = [x for x in (r.stdout + r.stderr).splitlines() if x.strip() and not x.startswith("schema ")]
        out += [f"{name}: {x.strip()}" for x in lines]
    return out


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


CONTAINER = os.environ.get("CORTEX_CONTAINER", "cortex-hub-1")   # the hub's container, for the stdio bridge


def mcp_setup(name: str, folder: str, kind: str, role: str, by: str, base: str, token: str) -> str:
    """What the person runs or pastes so the agent reaches its cortex over MCP: one line for its own client, nothing
    installed on the machine, nothing written to the agent's instruction files (the tool says how to use it)."""
    server = wire_agent.slug(name)
    server = server if "cortex" in server else f"cortex-{server}"   # the MCP server's name in the client: fern-cortex
    url = f"{base}/mcp/{quote(folder)}?by={quote(by)}" + ("&role=reader" if role == "reader" else "")
    auth = f"Bearer {token}"
    j = lambda o: json.dumps(o, indent=2)   # noqa: E731
    lines = {
        "claude-code": f'claude mcp add --transport http --scope user {server} "{url}" --header "Authorization: {auth}"',
        "codex": f'# ~/.codex/config.toml\n[mcp_servers.{server}]\nurl = "{url}"\n'
                 f'http_headers = {{ Authorization = "{auth}" }}',
        "gemini": '# ~/.gemini/settings.json, inside "mcpServers"\n' +
                  j({server: {"httpUrl": url, "headers": {"Authorization": auth}}}),
        "opencode": '# opencode.json, inside "mcp"\n' +
                    j({server: {"type": "remote", "url": url, "headers": {"Authorization": auth}}}),
    }
    generic = j({"mcpServers": {server: {"type": "http", "url": url, "headers": {"Authorization": auth}}}})
    stdio = (f'docker exec -i {CONTAINER} python3 /opt/cortex/scripts/cortex_mcp.py --vault "/vaults/{folder}" '
             f'--by {by}' + (" --role reader" if role == "reader" else ""))
    text = (f'Connect {by} to its long-term memory, the Cortex vault "{name}", over MCP. It is the {role}.\n'
            "Nothing to install: the vault and its engine live in the Cortex container. The token below is this "
            "vault's key: keep it private (a new one: ./cortex.sh token <vault> --rotate).\n\n")
    if kind in lines:
        text += f"===== {kind} =====\n{lines[kind]}\n\n"
    text += (f"===== any other MCP client (streamable HTTP) =====\n{generic}\n\n"
             f"===== a client that only speaks stdio =====\n{stdio}\n\n"
             "Then open a new session: the server tells the agent, as part of its instructions, that this cortex is "
             "its long-term memory, to start each task with action=brief and to end it with action=finish, which "
             f"commits what it learned under its name ({by}).\n")
    return text


def review_dir(root: Path, data: Path | None) -> Path:
    """Where scripts/review_prep.py leaves Strata's verdicts, one <vault>.jsonl each, and progress.json."""
    return (data or root / ".cortex-hub") / "review"


def review_list(root: Path, data: Path | None, shown: dict[str, Path]) -> dict:
    d = review_dir(root, data)
    out = []
    for name in shown:
        items = list(read_store(d / f"{name}.jsonl").values())
        if items:
            open_ = [i for i in items if not i.get("decided")]
            out.append({"name": name, "total": len(items), "open": len(open_),
                        "actions": {a: sum(i.get("action") == a for i in open_) for a in ("consolidate", "fix", "obsolete", "merge")}})
    return {"vaults": out, "progress": read_json(d / "progress.json", {})}


def read_store(path: Path) -> dict[str, dict]:
    have = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                have[d["id"]] = d
            except (ValueError, KeyError, TypeError):
                continue
    return have


def load_scopes(data: Path | None) -> dict[str, str]:
    """Each vault's scope, kept by the hub in DATA/scopes.json: local (default) or lan."""
    return read_json(data / "scopes.json", {}) if data else {}


def save_scope(data: Path, name: str, scope: str) -> None:
    scopes = load_scopes(data)
    scopes[name] = scope
    data.mkdir(parents=True, exist_ok=True)   # with --local-http and no LAN, no CA made the folder first
    tmp = data / "scopes.json.tmp"
    tmp.write_text(json.dumps(scopes, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(data / "scopes.json")


def connect(root: Path, host_root: str, form: dict, owner: str, lock: threading.Lock,
            data: Path | None = None, lan: bool = False, mcp_base: str | None = None) -> dict:
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
    if mcp_base:   # the hub serves MCP: the agent connects to it, nothing runs on this machine
        prompt = mcp_setup(name, folder, kind, role, by, mcp_base, cortex_mcp.ensure_token(v))
    else:
        prompt = agent_prompt(name, folder, kind, role, by, py, host_root, committed)
    return {"vault": name, "folder": folder, "scope": scope, "created": created, "log": log, "prompt": prompt,
            "mcp": bool(mcp_base)}


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


class Hub(cortex_mcp.McpRoutes, BaseHTTPRequestHandler):
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
    lan_tls = False                 # whether the LAN listener speaks HTTPS (it does whenever there is one)
    lan_public_port = 443           # the port LAN browsers use, for the links to the LAN listener
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

    def url(self, host: str, lan: bool = False) -> str:
        """A link to this listener, or with lan=True to the LAN listener: the two may differ in scheme and port
        (--local-http: plain HTTP on this machine, HTTPS on the local network)."""
        tls, port = (self.lan_tls, self.lan_public_port) if lan else (self.tls is not None, self.public_port)
        return f"https://{host}" + ("" if port == 443 else f":{port}") if tls else f"http://{host}:{port}"

    def vault_url(self, name: str, scope: str) -> str | None:
        """Where a vault's panel lives for its scope: http://claude.cortex.localhost:8790/ or https://claude.alastor.cortex.home/."""
        d = self.lan_domain if scope == "lan" else (self.domain if self.scope == "localhost" else None)
        return self.url(f"{subdomain(name)}.{d}", lan=scope == "lan") + "/" if d and "." in d else None

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
        limit = 262144 if urlsplit(self.path).path == "/api/review/apply" else limit   # a whole domain's decisions
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request too large")
        return self.rfile.read(n)

    def same_origin(self) -> bool:
        o = self.headers.get("Origin")
        return o is not None and (urlsplit(o).hostname or "") in self.allowed

    def is_mcp(self, path: str) -> bool:
        """/mcp/<vault>: the agents' MCP service, on this machine's listener only (--mcp), behind each vault's token."""
        return self.mcp_hub is not None and self.scope == "localhost" and path.startswith("/mcp/")

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if self.gate(path):
            return
        return self.mcp_delete() if self.is_mcp(path) else self.send(405, b"not allowed", TEXT)

    def do_POST(self):
        path = urlsplit(self.path).path
        if self.gate(path):
            return
        if self.is_mcp(path):   # an agent, not a browser: its token stands in for the page's same-origin checks
            return self.mcp_post()
        if path != "/login" and not self.same_origin():   # sign-in carries the key in the body: no Origin needed
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
                try:   # a by-IP sign-in gets a host-only cookie: a browser drops Domain= on a bare address
                    by_ip = bool(ipaddress.ip_address(self.hostname()))
                except ValueError:
                    by_ip = False
                shared = f"Domain={self.domain}; " if self.domain and "." in self.domain and not by_ip else ""   # the agents' hosts too
                return self.redirect("/", {"Set-Cookie": f"cortex_hub={tok}; {shared}Path=/; HttpOnly; Secure; "
                                                         f"SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"})
            time.sleep(1)   # slows guessing
            return self.send(HTTPStatus.UNAUTHORIZED, LOGIN.replace("<!--err-->", "<p class=err>Wrong key.</p>").replace("__CREDIT__", CREDIT).encode(),
                             "text/html; charset=utf-8")
        if path == "/api/review/apply":
            if self.scope != "localhost":   # only the owner, on this machine, marks nodes
                return self.send(HTTPStatus.FORBIDDEN, b"review from the hub on this machine", TEXT)
            if not hmac.compare_digest(self.headers.get("X-Cortex-Csrf") or "", self.csrf):
                return self.send(HTTPStatus.FORBIDDEN, b"stale page: reload it", TEXT)
            try:
                req = json.loads(raw or b"{}")
                v = vaults(self.root).get(str(req.get("vault") or ""))
                if v is None or not isinstance(req.get("decisions"), dict):
                    raise ValueError("pick a cortex and some decisions")
                r = subprocess.run([sys.executable, str(ENGINE / "scripts/review_apply.py"), str(v), "--owner", self.owner,
                                    "--out", str(review_dir(self.root, self.data))],
                                   input=json.dumps({"decisions": req["decisions"]}), capture_output=True, text=True, timeout=600)
                out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-1500:]}
            except (ValueError, subprocess.TimeoutExpired) as e:
                return self.send(HTTPStatus.BAD_REQUEST, json.dumps({"error": str(e)}).encode(), "application/json")
            return self.send(HTTPStatus.INTERNAL_SERVER_ERROR if "error" in out else 200, json.dumps(out).encode(), "application/json")
        if path == "/api/connect":
            if self.scope != "localhost":   # vaults live on this machine: only its owner connects agents
                return self.send(HTTPStatus.FORBIDDEN, b"connect agents from the hub on this machine", TEXT)
            if not hmac.compare_digest(self.headers.get("X-Cortex-Csrf") or "", self.csrf):
                return self.send(HTTPStatus.FORBIDDEN, b"stale page: reload it", TEXT)
            try:
                out = connect(self.root, self.host_root or str(self.root), json.loads(raw or b"{}"), self.owner, self.creating,
                              self.data, self.lan_domain is not None,
                              self.url(self.domain if self.domain and "." in self.domain else "localhost")
                              if self.mcp_hub is not None else None)
                out["url"] = self.vault_url(out["folder"], out["scope"])
            except ValueError as e:
                return self.send(HTTPStatus.BAD_REQUEST, json.dumps({"error": str(e)}).encode(), "application/json")
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                return self.send(HTTPStatus.INTERNAL_SERVER_ERROR, json.dumps({"error": str(e)}).encode(), "application/json")
            return self.send(200, json.dumps(out).encode(), "application/json")
        self.send(404, b"not found", TEXT)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.gate(path):
            return
        if self.is_mcp(path):
            return self.mcp_get()
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
            return self.send(200, LOGIN.replace("__CREDIT__", CREDIT).encode(), "text/html; charset=utf-8") if self.key else self.redirect("/")
        if path == "/ca.crt" and self.tls:
            return self.send(200, (self.tls / "ca.crt").read_bytes(), "application/x-x509-ca-cert",
                             {"Content-Disposition": 'attachment; filename="cortex-hub-ca.crt"'})
        if path in VIEWS or path.startswith("/panel/"):   # one shell; the client picks the view
            return self.send(200, PAGE.replace("__CSRF__", self.csrf).replace("__CREDIT__", CREDIT).encode("utf-8"),
                             "text/html; charset=utf-8")
        if path == "/api/review" and self.scope != "localhost":
            return self.send(HTTPStatus.FORBIDDEN, b"review from the hub on this machine", TEXT)
        if path == "/api/review":
            name = (parse_qs(urlsplit(self.path).query).get("vault") or [""])[0]
            if not name:
                return self.send(200, json.dumps(review_list(self.root, self.data, vaults(self.root))).encode(), "application/json")
            if name not in vaults(self.root):
                return self.send(404, b"no such cortex", TEXT)
            items = list(read_store(review_dir(self.root, self.data) / f"{name}.jsonl").values())
            return self.send(200, json.dumps({"vault": name, "items": items}, ensure_ascii=False).encode(), "application/json")
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
                    d["ip_url"] = self.url(self.lan_ip, lan=True) + f"/v/{subdomain(d['name'])}/"
            hub = {"scope": self.scope, "tls": self.tls is not None, "login": self.key is not None, "domain": self.domain,
                   "lan": self.lan_domain if self.scope == "localhost" else None,
                   "connect": self.scope == "localhost" and wire_agent is not None and (ENGINE / "scripts/new_vault.py").is_file(),
                   "mcp": self.scope == "localhost" and self.mcp_hub is not None}
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


# the bar every hub page shares: each route one click away, the cortexes' panels in a menu
# the hub's bar: each view one click away, the cortexes' panels in a menu; the page's script fills it
NAV = r"""<nav class="topnav" aria-label="Hub">
  <a class="home" href="/" data-link>Cortex hub <span class="brand">By MadLeak</span></a>
  <div class="links">
    <a href="/agents" id="m-grid" data-nav="agents" data-link>Agents</a>
    <a href="/wall" id="m-wall" data-nav="wall" data-link>Wall</a>
    <a href="/review" data-nav="review" id="nav-review" data-link hidden>Review <span class="count" id="nav-review-n"></span></a>
    <details class="menu" id="nav-menu" data-nav="panel"><summary>Panels</summary><div class="drop" id="nav-vaults"></div></details>
    <a href="/ca.crt" id="nav-ca" hidden title="The hub's local CA, to trust it on another device">CA</a>
  </div>
  <div class="tools">
    <a class="btn primary" href="/connect" id="nav-connect" data-link hidden>Connect an agent</a>
    <button type="button" id="theme">Theme</button>
  </div>
</nav>"""


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
.actions { position: relative; display: flex; gap: 6px; margin-top: auto; align-items: center; }
.actions .btn { flex: 0 0 auto; white-space: nowrap; }
.host { position: absolute; right: 0; bottom: calc(100% + 6px); z-index: 2; margin: 0; padding: 2px 7px; max-width: 100%;
  font-size: 11px; color: var(--ink2); background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
  box-shadow: var(--shadow);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; pointer-events: none; opacity: 0; transition: opacity .12s ease; }
.actions:hover .host, .card:focus-within .host { opacity: 1; }
.empty { color: var(--muted); font-size: 13px; }
/* the wall: every agent's panel at once, orbit only */
.wall { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr)); gap: 12px; }
.frame { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
.frame .bar { display: flex; align-items: center; gap: 8px; padding: 6px 10px; font-size: 13px; font-weight: 600; }
.frame .bar a { margin-left: auto; }
.frame iframe { border: 0; width: 100%; height: min(70vh, 640px); display: block; background: var(--page); }
.foot { margin-top: 18px; color: var(--muted); font-size: 12px; }
.foot a { color: var(--ink2); }
.credit { margin-top: 4px; }
.by, .brand { font-weight: 650; letter-spacing: .02em; color: var(--ink); }
h1 .brand { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted);
  border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; margin-left: 6px; vertical-align: 3px; }
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
.topnav { display: flex; align-items: center; gap: 6px 14px; flex-wrap: wrap; padding: 8px 12px; margin-bottom: 12px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; position: sticky; top: 8px; z-index: 5; }
.topnav a { color: var(--ink2); text-decoration: none; }
.topnav .home { font-weight: 650; color: var(--ink); font-size: 15px; }
.topnav .home .brand { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted);
  border: 1px solid var(--border); border-radius: 999px; padding: 1px 7px; margin-left: 4px; vertical-align: 2px; }
.topnav .links { display: flex; gap: 2px; align-items: center; flex-wrap: wrap; }
.topnav .links > a, .topnav summary { padding: 4px 10px; border-radius: 999px; font-size: 13px; cursor: pointer; }
.topnav .links > a:hover, .topnav summary:hover { background: var(--wash); color: var(--ink); }
.topnav [aria-current] { background: var(--ink); color: var(--surface) !important; }
.topnav .count { font-size: 11px; font-variant-numeric: tabular-nums; background: var(--warning); color: #0b0b0b; border-radius: 999px; padding: 0 6px; margin-left: 2px; }
.topnav .count:empty { display: none; }
.topnav .tools { margin-left: auto; display: flex; gap: 6px; }
.menu { position: relative; }
.menu summary { list-style: none; color: var(--ink2); }
.menu summary::-webkit-details-marker { display: none; }
.menu summary::after { content: " \25BE"; font-size: 10px; }
.menu .drop { position: absolute; top: calc(100% + 6px); left: 0; min-width: 180px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; box-shadow: var(--shadow); padding: 4px; display: grid; }
.menu .drop a { padding: 6px 10px; border-radius: 6px; font-size: 13px; color: var(--ink); }
.menu .drop a:hover { background: var(--wash); }
.menu .drop a.live::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--good); margin-right: 6px; }
.menu .drop .pv { display: flex; align-items: center; }
.menu .drop .pv a:first-child { flex: 1; }
.menu .drop .pv .ext { padding: 6px 8px; font-size: 11px; color: var(--muted); }
#panel iframe { height: max(480px, calc(100vh - 120px)); }
.tabs { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0; }
.intro { color: var(--ink2); font-size: 13px; max-width: 80ch; margin: 0 0 6px; }
.prog { font-size: 12px; color: var(--ink2); display: flex; gap: 8px; align-items: center; }
.prog meter { width: 160px; }
.dom { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; margin: 12px 0; overflow: hidden; }
.dom > header { margin: 0; padding: 8px 12px; border-bottom: 1px solid var(--border); gap: 8px; }
.dom h2 { font-size: 14px; margin: 0; }
.item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 14px; padding: 10px 12px; border-top: 1px solid var(--border); }
.item:first-of-type { border-top: 0; }
.item .t { font-weight: 600; }
.item .meta { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.item .sum { color: var(--ink2); font-size: 12px; }
.item .why { font-size: 12px; }
.chip { display: inline-block; font-size: 11px; border-radius: 999px; padding: 1px 7px; border: 1px solid var(--border); margin-right: 4px; }
.a-consolidate { color: var(--good); border-color: currentColor; }
.a-fix, .a-merge { color: #b07a00; border-color: currentColor; }
.a-obsolete { color: #d03b3b; border-color: currentColor; }
.fail { color: var(--muted); }
.pick { display: flex; gap: 4px; align-items: start; flex-wrap: wrap; justify-content: flex-end; }
.pick button[aria-pressed="true"].d-consolidated { background: var(--good); border-color: var(--good); color: #fff; }
.pick button[aria-pressed="true"].d-obsolete { background: #d03b3b; border-color: #d03b3b; color: #fff; }
.done { color: var(--muted); font-size: 12px; }
.rbar { position: sticky; bottom: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  box-shadow: var(--shadow); padding: 10px 12px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
.rbar .n { margin-right: auto; font-size: 13px; }
@media (max-width: 640px) { .item { grid-template-columns: 1fr; } .pick { justify-content: flex-start; } }
@media (max-width: 640px) { .topnav .tools { margin-left: 0; } .topnav { position: static; } }
</style>
</head>
<body>
<div class="wrap">
  __NAV__
  <div class="sub" id="sub">Loading…</div>
  <main>
    <section class="grid" id="grid" aria-label="One card per agent"></section>
    <section class="wall" id="wall" aria-label="Every agent's panel" hidden></section>
    <section id="review" aria-label="Review" hidden>
      <div class="prog" id="prog"></div>
      <p class="intro">Strata, the local model, judged each seed with its parent and siblings in view. Its verdict is only a
      suggestion: nothing in the cortex changes until you press <b>Apply</b>. Consolidated and obsolete change the node's
      status in one commit; fix and skip are only recorded, and the node is judged again once its text changes.</p>
      <nav class="tabs" id="vaults" aria-label="Cortexes"></nav>
      <nav class="tabs" id="filters" aria-label="Strata's verdict"></nav>
      <div id="list"><p class="empty">Loading…</p></div>
      <div class="rbar" id="bar" hidden>
        <span class="n" id="count"></span>
        <button type="button" id="clear">Clear</button>
        <button type="button" class="primary" id="apply">Apply</button>
      </div>
      <p class="err" id="msg" role="status"></p>
    </section>
    <section id="panel" aria-label="One agent's panel" hidden></section>
  </main>
  <p class="foot" id="foot"></p>
  <p class="foot credit">__CREDIT__</p>
</div>
<dialog id="connect" aria-labelledby="c-title">
  <form id="c-form" class="form-wrap">
    <h2 id="c-title">Connect an agent</h2>
    <div class="sub" id="c-sub">The hub prepares the cortex; the agent wires itself from the prompt you paste to it.</div>
    <div class="form">
      <label>Agent name <input name="agent" required maxlength="40" pattern="[A-Za-z0-9][A-Za-z0-9 ._\-]*" placeholder="Codex" autocomplete="off"></label>
      <label>Agent
        <select name="kind">
          <option value="claude-code">Claude Code</option><option value="codex">Codex CLI</option>
          <option value="gemini">Gemini CLI</option><option value="opencode">OpenCode</option>
          <option value="hermes">Hermes Agent</option><option value="custom" id="c-custom">Another agent (it names its own instruction file)</option>
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
      <label id="c-python-row">Python on the agent's machine
        <select name="python"><option>python3</option><option>python</option><option value="py">py (Windows launcher)</option></select></label>
    </div>
    <p class="err" id="c-err" role="alert"></p>
    <div class="row"><button type="button" id="c-cancel">Cancel</button><button type="submit" class="primary" id="c-go">Create the prompt</button></div>
  </form>
  <div id="c-out" hidden>
    <h2><span id="c-out-lead">Paste this to</span> <span id="c-who"></span><span id="c-out-tail"></span></h2>
    <textarea id="c-text" readonly aria-label="Prompt for the agent"></textarea>
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
  $('#theme').addEventListener('click', () => {
    root.dataset.theme = theme() === 'dark' ? 'light' : 'dark'; store.set('theme', root.dataset.theme);
    if (view === 'wall') renderWall(true); else if (view === 'panel') renderPanel(true);
  });
  const hhmm = t => String(t || '').slice(11, 16);
  const ago = t => {
    if (!t) return 'no activity yet';
    const m = Math.round((Date.now() - Date.parse(t)) / 60000);
    return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
  };
  const panelUrl = (v, extra = '') => `/v/${v.slug}/Panel.html${extra}`;   // same origin: the wall embeds these
  const byIp = /^\d{1,3}(\.\d{1,3}){3}$/.test(location.hostname);   // a sign-in by address is host-only: stay on it
  const openUrl = (v, extra = '') => byIp && v.ip_url ? v.ip_url + extra : (v.url ? v.url + extra : panelUrl(v, extra));
  let hubInfo = {}, vaults = [], loaded = false, view = 'agents', slug = '';
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
    hs.textContent = byIp && v.ip_url ? `LAN · ${new URL(v.ip_url).host}`
      : v.lan_url ? `LAN · ${new URL(v.lan_url).host}` : v.url ? new URL(v.url).host : '';
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
  const short = v => v.name.replace(/ Cortex$/, '');
  const dec = s => { try { return decodeURIComponent(s); } catch (e) { return s; } };
  const findVault = s => vaults.find(v => dec(v.slug) === dec(s));
  let panelSlug = null, reviewApi = null;
  function renderPanel(force) {
    if (!loaded) return;
    const p = $('#panel'), v = findVault(slug);
    if (!v) { panelSlug = null; p.replaceChildren(); el('p', { class: 'empty' }, 'No such cortex', p); return; }
    const src = panelUrl(v, `?embed&theme=${theme()}`), cur = p.querySelector('iframe');
    if (cur && panelSlug === slug) { if (force) cur.src = src; return; }
    panelSlug = slug; p.replaceChildren();
    const f = el('section', { class: 'frame' }, null, p);
    const bar = el('div', { class: 'bar' }, null, f);
    el('span', {}, short(v), bar);
    el('a', { class: 'btn', href: openUrl(v), target: '_blank', rel: 'noopener' }, 'Open in a new tab', bar);
    el('iframe', { title: `${v.name} panel`, src }, null, f);
  }
  function leavePanel() {
    const f = $('#panel iframe');
    if (f) f.src = 'about:blank';   // a hidden panel would keep animating
    panelSlug = null;
  }
  function go(path, push = true) {
    if (path === '/review' && loaded && hubInfo.scope !== 'localhost') path = '/agents';
    if (path !== location.pathname) history[push ? 'pushState' : 'replaceState'](null, '', path);
    const m = path.match(/^\/panel\/([^/]*)\/?$/);
    const next = m ? 'panel' : path === '/wall' ? 'wall' : path === '/review' ? 'review' : 'agents';
    if (view === 'panel' && next !== 'panel') leavePanel();
    view = next; slug = m ? m[1] : '';
    $('#grid').hidden = next !== 'agents'; $('#wall').hidden = next !== 'wall';
    $('#review').hidden = next !== 'review'; $('#panel').hidden = next !== 'panel';
    $('#sub').hidden = next === 'review' || next === 'panel';
    document.querySelectorAll('.topnav [data-nav]').forEach(a => a.toggleAttribute('aria-current', a.dataset.nav === next));
    const found = findVault(slug);
    document.title = 'Cortex hub · ' + (next === 'panel' ? (found ? short(found) : dec(slug).replace(/ Cortex$/, '')) :
      { agents: 'Agents', wall: 'Wall', review: 'Review' }[next]);
    if (next === 'wall') renderWall(); else { $('#wall').replaceChildren(); wallTheme = null; }   // a hidden wall would keep seven panels animating
    if (next === 'panel') renderPanel();
    if (next === 'review' && loaded) { if (reviewApi) reviewApi.refresh(); else reviewApi = initReview(); }
    if (path === '/connect' && loaded) { if (!hubInfo.connect) history.replaceState(null, '', '/agents'); else if (!dlg.open) openConnect(); }
    else if (dlg.open) dlg.close();
  }
  const menu = $('#nav-menu');
  document.addEventListener('click', e => {
    if (!menu.contains(e.target) || e.target.closest('a')) menu.open = false;
    const a = e.target.closest('a[data-link]');
    if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault(); go(a.getAttribute('href'));
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.open = false; });
  addEventListener('popstate', () => go(location.pathname, false));
  let navKey = '';
  function fillNav() {
    const key = JSON.stringify(vaults.map(v => [v.slug, v.name, v.url, v.live, v.nodes]));
    if (key !== navKey) {
      navKey = key;
      const box = $('#nav-vaults'); box.replaceChildren();
      for (const v of vaults) {
        const pv = el('div', { class: 'pv' }, null, box);
        el('a', { class: v.live ? 'live' : '', href: `/panel/${v.slug}`, 'data-link': '', title: `${v.name} · ${v.nodes} nodes` }, short(v), pv);
        el('a', { class: 'ext', href: v.url || `/v/${v.slug}/`, target: '_blank', rel: 'noopener', title: `Open ${v.name} on its own host`, 'aria-label': `Open ${v.name} in a new tab` }, '↗', pv);
      }
    }
    $('#nav-review').hidden = hubInfo.scope !== 'localhost';
    $('#nav-connect').hidden = !hubInfo.connect;
    $('#nav-ca').hidden = !hubInfo.tls;
  }
  async function reviewCount() {
    if (hubInfo.scope !== 'localhost') return;
    try {
      const r = await (await fetch('/api/review', { cache: 'no-store' })).json();
      const n = r.vaults.reduce((s, v) => s + v.open, 0);
      $('#nav-review-n').textContent = n ? String(n) : '';
    } catch (e) { /* the page itself reports a hub that does not answer */ }
  }
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch('/api/vaults', { cache: 'no-store' });
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json(); vaults = j.vaults; hubInfo = j.hub;
      const first = !loaded; loaded = true;
      $('#foot').replaceChildren(`${j.hub.scope === 'lan' ? 'Local network: the cortexes marked LAN' : 'This machine: every cortex'} · ${j.hub.tls ? 'HTTPS by the hub\'s own CA · ' : 'plain HTTP'}`);
      if (j.hub.tls) el('a', { href: '/ca.crt' }, 'download ca.crt to trust it', $('#foot'));
      const live = vaults.filter(v => v.live).length;
      $('#sub').textContent = `${vaults.length} agents · ${live} active in the last 10 minutes · ${vaults.reduce((s, v) => s + v.nodes, 0)} nodes`;
      renderGrid(); fillNav();
      if (first) { go(location.pathname, false); reviewCount(); }
      else if (view === 'wall') renderWall();
      else if (view === 'panel') renderPanel();
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
  function mode() {
    // a hub that serves MCP: the person adds a config line to the agent's MCP client, nothing runs on this machine
    const mcp = !!hubInfo.mcp;
    $('#c-python-row').hidden = mcp;
    $('#c-sub').textContent = mcp
      ? 'The hub prepares the cortex and its token. Add the config it gives you to the agent\'s MCP client: nothing is installed or written on this machine.'
      : 'The hub prepares the cortex; the agent wires itself from the prompt you paste to it.';
    $('#c-custom').textContent = mcp ? 'Another MCP client (generic config)' : 'Another agent (it names its own instruction file)';
    $('#c-go').textContent = mcp ? 'Create the config' : 'Create the prompt';
    $('#c-out-lead').textContent = mcp ? 'Add this to' : 'Paste this to';
    $('#c-out-tail').textContent = mcp ? '\u2019s MCP client' : '';
    $('#c-text').setAttribute('aria-label', mcp ? 'MCP configuration for the agent\'s client' : 'Prompt for the agent');
  }
  function sync() {
    mode();
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
      $('#c-text').value = out.prompt;
      cform.hidden = true; $('#c-out').hidden = false; $('#c-copy').textContent = 'Copy';
      poll();
    } catch (e) { $('#c-err').textContent = String(e.message || e); }
    finally { $('#c-go').disabled = false; }
  });
  $('#c-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#c-text').value); $('#c-copy').textContent = 'Copied'; }
    catch (e) { $('#c-text').select(); $('#c-copy').textContent = 'Press Ctrl+C'; }
  });
  dlg.addEventListener('close', () => { if (location.pathname === '/connect') history.replaceState(null, '', '/agents'); });
  $('#c-cancel').addEventListener('click', () => dlg.close());
  $('#c-done').addEventListener('click', () => { dlg.close(); cform.reset(); delete cform.dataset.touched; });

  function initReview() {
    const SUGGEST = { consolidate: 'consolidated', obsolete: 'obsolete', fix: 'fix', merge: 'fix' };
    const CHOICES = [['consolidated', 'Consolidate'], ['obsolete', 'Obsolete'], ['fix', 'Fix later'], ['skip', 'Skip']];
    const FILTERS = [['open', 'To review'], ['consolidate', 'Strata: consolidate'], ['fix', 'Strata: fix'], ['merge', 'Strata: merge'],
                     ['obsolete', 'Strata: obsolete'], ['decided', 'Decided']];
    let vault = store.get('review-vault', ''), filter = 'open', items = [];
    const picks = new Map();   // node id -> decision, until applied

    function tabs(parent, list, current, onPick) {
      parent.replaceChildren();
      for (const [k, label] of list) {
        const b = el('button', { type: 'button', 'aria-pressed': String(k === current) }, label, parent);
        b.addEventListener('click', () => onPick(k));
      }
    }
    function count() {
      const n = {}; for (const d of picks.values()) n[d] = (n[d] || 0) + 1;
      $('#bar').hidden = !picks.size;
      $('#count').textContent = `${picks.size} decisions: ` + CHOICES.filter(([k]) => n[k]).map(([k, l]) => `${n[k]} ${l.toLowerCase()}`).join(', ');
    }
    function shown() {
      if (filter === 'decided') return items.filter(i => i.decided);
      const open = items.filter(i => !i.decided);
      return filter === 'open' ? open : open.filter(i => i.action === filter);
    }
    function row(i, parent) {
      const r = el('div', { class: 'item' }, null, parent);
      const info = el('div', {}, null, r);
      el('div', { class: 't' }, i.title, info);
      el('div', { class: 'meta' }, `L${i.layer} · ${i.truth || 'no truth'} · ${i.reads} reads · ${i.used} credited`, info);
      if (i.summary) el('div', { class: 'sum' }, i.summary, info);
      const why = el('div', { class: 'why' }, null, info);
      el('span', { class: `chip a-${i.action}` }, `Strata: ${i.action}${i.target ? ' into ' + i.target : ''}`, why);
      why.append(i.reason || '');
      const failed = Object.entries(i.checks || {}).filter(([, ok]) => !ok).map(([k]) => k.replace(/_/g, ' '));
      if (failed.length) el('div', { class: 'meta fail' }, 'fails: ' + failed.join(', '), info);
      const pick = el('div', { class: 'pick' }, null, r);
      if (i.decided) { el('span', { class: 'done' }, `${i.decided} · ${String(i.decided_t || '').replace('T', ' ')}`, pick); return; }
      for (const [k, label] of CHOICES) {
        const b = el('button', { type: 'button', class: `d-${k}`, 'aria-pressed': String(picks.get(i.id) === k) }, label, pick);
        b.addEventListener('click', () => {
          picks.get(i.id) === k ? picks.delete(i.id) : picks.set(i.id, k);
          for (const x of pick.children) x.setAttribute('aria-pressed', String(picks.get(i.id) === x.className.slice(2)));
          count();
        });
      }
    }
    function render() {
      tabs($('#filters'), FILTERS.map(([k, l]) => [k, `${l} (${k === 'decided' ? items.filter(i => i.decided).length :
        items.filter(i => !i.decided && (k === 'open' || i.action === k)).length})`]), filter, k => { filter = k; render(); });
      const list = $('#list'); list.replaceChildren();
      const rows = shown();
      if (!rows.length) { el('p', { class: 'empty' }, vault ? 'Nothing here.' : 'Strata has not judged any cortex yet: run scripts/review_prep.py.', list); return; }
      const groups = {};
      for (const i of rows) (groups[i.domain || '(top)'] ||= []).push(i);
      for (const [dom, its] of Object.entries(groups).sort((a, b) => b[1].length - a[1].length)) {
        const sec = el('section', { class: 'dom' }, null, list);
        const h = el('header', {}, null, sec);
        el('h2', {}, `${dom} · ${its.length}`, h);
        if (filter !== 'decided') {
          const all = el('button', { type: 'button', title: "Consolidate, obsolete or fix each one as Strata says; you can still change any" },
                         "Take Strata's verdicts", h);
          all.addEventListener('click', () => { for (const i of its) picks.set(i.id, SUGGEST[i.action] || 'fix'); render(); count(); });
        }
        const body = el('div', {}, null, sec);
        for (const i of its) row(i, body);
      }
    }
    async function load() {
      const j = await (await fetch('/api/review', { cache: 'no-store' })).json();
      const p = j.progress || {};
      const prog = $('#prog'); prog.replaceChildren();
      if (p.task && p.total) {
        prog.append(`Strata (subcontracted): ${p.task} ${p.done}/${p.total}`);
        el('meter', { min: 0, max: p.total, value: p.done }, null, prog);
      } else prog.append('Strata is idle');
      if (!j.vaults.find(v => v.name === vault)) vault = (j.vaults[0] || {}).name || '';
      tabs($('#vaults'), j.vaults.map(v => [v.name, `${v.name} · ${v.open} open`]), vault, k => { vault = k; store.set('review-vault', k); picks.clear(); count(); load(); });
      if (!vault) { items = []; return render(); }
      items = (await (await fetch('/api/review?vault=' + encodeURIComponent(vault), { cache: 'no-store' })).json()).items || [];
      const order = { consolidate: 0, merge: 1, fix: 2, obsolete: 3 };
      items.sort((a, b) => (order[a.action] - order[b.action]) || (b.used * 3 + b.reads) - (a.used * 3 + a.reads));
      render();
    }
    $('#clear').addEventListener('click', () => { picks.clear(); count(); render(); });
    $('#apply').addEventListener('click', async () => {
      const decisions = Object.fromEntries(picks);
      const n = Object.values(decisions).filter(d => d === 'consolidated' || d === 'obsolete').length;
      if (n && !confirm(`Change the status of ${n} nodes in ${vault} and commit it?`)) return;
      $('#apply').disabled = true; $('#msg').textContent = 'Applying…';
      try {
        const r = await fetch('/api/review/apply', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Cortex-Csrf': csrf },
                                                       body: JSON.stringify({ vault, decisions }) });
        const out = await r.json().catch(() => ({ error: `The hub answered ${r.status}` }));
        if (!r.ok) throw new Error(out.error || r.status);
        $('#msg').textContent = `${out.decided} decisions recorded, ${out.changed} nodes changed` + (out.commit ? ` in commit ${out.commit}` : '') + '.';
        picks.clear(); count(); await load();
      } catch (e) { $('#msg').textContent = String(e.message || e); }
      finally { $('#apply').disabled = false; }
    });
    load();
    return { refresh() { if (!document.hidden && !picks.size) load(); } };
  }
  setInterval(() => { if (view === 'review' && reviewApi) reviewApi.refresh(); }, 30000);
  setInterval(() => document.hidden || reviewCount(), 30000);
  if (location.hash === '#wall') history.replaceState(null, '', '/wall');
  else if (location.hash === '#agents') history.replaceState(null, '', '/agents');
  else if (location.hash === '#connect') history.replaceState(null, '', '/connect');
  go(location.pathname, false);
  poll(); setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__NAV__", NAV)


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
.credit { position: fixed; bottom: 12px; left: 0; right: 0; text-align: center; }
.credit a { color: inherit; }
.by { font-weight: 650; }
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
<p class="credit">__CREDIT__</p>
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
    ap.add_argument("--mcp", action="store_true",
                    help="also serve MCP at /mcp/<vault> on this machine's listener, each vault behind its own token "
                         "(<vault>/_brain/state/mcp.token, made on start)")
    ap.add_argument("--local-http", action="store_true",
                    help="with --data, keep this machine's listener in plain HTTP (no CA to trust: browsers already "
                         "treat localhost as secure); only the LAN listener speaks HTTPS")
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
    lan.add_argument("--lan-public-port", type=int, default=443, help="port LAN browsers use (443: none in the links)")
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
    if a.local_http and scope != "localhost":
        sys.exit("--local-http is for a loopback --address: the local network always gets HTTPS")
    Hub.root, Hub.data = Path(a.root).expanduser(), Path(a.data).expanduser() if a.data else None
    Hub.host_root = a.host_root or str(Hub.root)
    Hub.public_port = a.public_port or a.port
    Hub.lan_domain = lan_dns[0] if a.lan_address else None
    Hub.lan_ip = a.lan_address
    Hub.lan_public_port = a.lan_public_port
    ctx, scheme = None, "http"
    if Hub.data and not (a.local_http and not a.lan_address):   # --local-http with no LAN listener: no CA at all
        names = dns + (lan_dns if a.lan_address else [])
        zones = list(dict.fromkeys([dns[0], a.lan_domain, "localhost"]))   # the LAN zone even before a LAN listener
        crt, crt_key, done = hub_tls.ensure(Hub.data / "tls", list(dict.fromkeys(names)),
                                            list(dict.fromkeys(ips + (lan_ips if a.lan_address else []))), zones)
        for line in done:
            print(line, flush=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(crt, crt_key)
        Hub.tls, Hub.lan_tls, scheme = Hub.data / "tls", True, "https"
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
    main_ctx = None if a.local_http else ctx
    if a.local_http:
        scheme = "http"
    main_hub = type("MainHub", (Hub,), {"scope": scope, "domain": dns[0], "allowed": own(dns) | set(ips),
                                        "key": key if scope == "lan" else None,
                                        "tls": Hub.tls if main_ctx else None})
    httpd = listen(main_hub, a.host, a.port, main_ctx, a.redirect_port)
    print(f"cortex hub, {scope} scope, {scheme} for {', '.join(dns + ips)} · listening on {a.host}:{a.port} · "
          f"{len(vaults(Hub.root))} vaults under {Hub.root}", flush=True)
    if a.mcp:
        Hub.mcp_hub, Hub.mcp_require_token = cortex_mcp.Hub(Hub.root, 60.0, commit=True), True
        cortex_mcp.RESIDENT = cortex_mcp.Resident(ENGINE / "template-vault" / "_brain" / "scripts",
                                                  scan_seconds=float(env("CORTEX_SCAN_SECONDS") or 5))
        for line in upgrade_engines(Hub.root, cortex_mcp.RESIDENT.version) + migrate_vaults(Hub.root):
            print(line, flush=True)
        for v in vaults(Hub.root).values():
            cortex_mcp.ensure_token(v)
        port = "" if (scheme, Hub.public_port) in (("https", 443), ("http", 80)) else f":{Hub.public_port}"
        print(f"MCP at {scheme}://{dns[0]}{port}/mcp/<vault>, each vault behind the token in "
              f"<vault>/{cortex_mcp.TOKEN_IGNORE} · resident engine v{cortex_mcp.RESIDENT.version}, "
              f"file lists refreshed every {cortex_mcp.RESIDENT.scan_seconds:g}s", flush=True)
    if a.dns_port:
        import hub_dns
        zones = {dns[0]: address, **({lan_dns[0]: a.lan_address} if a.lan_address else {})}   # this machine knows both
        hub_dns.serve(a.host, a.dns_port, zones)
        print(f"DNS on {a.host}:{a.dns_port}/udp: " + ", ".join(f"*.{d} = {ip}" for d, ip in zones.items()), flush=True)
    if a.lan_address:
        lan_hub = type("LanHub", (Hub,), {"scope": "lan", "domain": lan_dns[0], "key": key,
                                          "allowed": own(lan_dns) | set(lan_ips), "public_port": a.lan_public_port})
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
