#!/usr/bin/env python3
"""Cortex hub: one web page with every agent's brain panel, live.

    python3 scripts/cortex_hub.py [--root ~/Vaults] [--host 127.0.0.1] [--port 8790]

Every directory under the root with _brain/scripts/brain.py is a vault (CORTEX_VAULTS changes the root, as for the
life cycle). The hub is read-only and serves only what the panels need: each vault's Panel.html, its live feed
_brain/state/activity.js and the orbit.svg thumbnail. No note, config or usage log leaves the vault.

Routes: / the overview (one card per agent, or the wall: every panel embedded at once) · /api/vaults the figures the
overview polls · /v/<vault>/Panel.html, /v/<vault>/state/activity.js, /v/<vault>/generated/orbit.svg.

It listens on localhost by default. The panels hold the vaults' summaries: put it behind an authenticated proxy
before it answers anyone else.
"""
import argparse
import json
import os
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ACTIVITY_PREFIX = "window.BRAIN_ACTIVITY = "
# the only files of a vault the hub serves, relative to its _brain folder
SERVED = {"Panel.html": "text/html; charset=utf-8", "state/activity.js": "text/javascript; charset=utf-8",
          "generated/orbit.svg": "image/svg+xml"}
LIVE_MINUTES = 10   # a vault with activity this recent shows as live


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


class Hub(BaseHTTPRequestHandler):
    root: Path = Path.home() / "Vaults"
    server_version = "CortexHub/1"

    def log_message(self, fmt, *args):   # quiet: the panels poll every few seconds
        pass

    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/vaults":
            data = [summary(n, v) for n, v in vaults(self.root).items()]
            return self.send(200, json.dumps({"vaults": data, "t": datetime.now().isoformat(timespec="seconds")}).encode(), "application/json")
        m = re.fullmatch(r"/v/([^/]+)/(.*)", path)
        if m:
            name, rel = unquote(m.group(1)), m.group(2) or "Panel.html"
            v = vaults(self.root).get(name)
            if v and rel in SERVED:
                f = v / "_brain" / rel
                if f.is_file():
                    return self.send(200, f.read_bytes(), SERVED[rel])
                if rel == "state/activity.js":   # no activity yet: an empty feed, not an error the panel would report
                    return self.send(200, b"", SERVED[rel])
            if v and not m.group(2):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", f"/v/{quote(name)}/Panel.html")
                self.end_headers()
                return
        self.send(404, b"not found", "text/plain; charset=utf-8")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cortex hub</title>
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
.actions { display: flex; gap: 6px; margin-top: auto; }
.empty { color: var(--muted); font-size: 13px; }
/* the wall: every agent's panel at once, orbit only */
.wall { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr)); gap: 12px; }
.frame { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
.frame .bar { display: flex; align-items: center; gap: 8px; padding: 6px 10px; font-size: 13px; font-weight: 600; }
.frame .bar a { margin-left: auto; }
.frame iframe { border: 0; width: 100%; height: min(70vh, 640px); display: block; background: var(--page); }
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
    </div>
  </header>
  <main>
    <section class="grid" id="grid" aria-label="One card per agent"></section>
    <section class="wall" id="wall" aria-label="Every agent's panel" hidden></section>
  </main>
</div>
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
  const panelUrl = (v, extra = '') => `/v/${v.slug}/Panel.html${extra}`;
  let vaults = [], mode = store.get('mode', 'grid');
  const seq = {}, cards = {};

  function card(v) {
    const c = el('article', { class: 'card' });
    const h = el('h2', {}, null, c);
    el('span', { class: 'pulse', 'aria-hidden': 'true' }, null, h);
    el('span', { class: 'n' }, v.name.replace(/ Cortex$/, ''), h);
    el('span', { class: 'v' }, null, h);
    const a = el('a', { href: panelUrl(v), target: '_blank', rel: 'noopener', 'aria-label': `Open the ${v.name} panel` }, null, c);
    el('img', { class: 'thumb', alt: `Orbit of ${v.name}`, loading: 'lazy', src: `/v/${v.slug}/generated/orbit.svg` }, null, a);
    el('div', { class: 'stats' }, null, c);
    el('ul', { class: 'feed', 'aria-label': 'Latest activity' }, null, c);
    const act = el('div', { class: 'actions' }, null, c);
    el('a', { class: 'btn', href: panelUrl(v), target: '_blank', rel: 'noopener' }, 'Open panel', act);
    el('a', { class: 'btn', href: panelUrl(v, '#3d'), target: '_blank', rel: 'noopener' }, '3D', act);
    return c;
  }
  function fill(c, v) {
    c.classList.toggle('live', !!v.live);
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
      el('a', { class: 'btn', href: panelUrl(v), target: '_blank', rel: 'noopener' }, 'Open', bar);
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
      vaults = (await r.json()).vaults;
      const live = vaults.filter(v => v.live).length;
      $('#sub').textContent = `${vaults.length} agents · ${live} active in the last 10 minutes · ${vaults.reduce((s, v) => s + v.nodes, 0)} nodes`;
      renderGrid();
      if (mode === 'wall') renderWall();
    } catch (e) { $('#sub').textContent = 'The hub is not answering'; }
  }
  setMode(mode);
  poll(); setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.environ.get("CORTEX_VAULTS", str(Path.home() / "Vaults")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    Hub.root = Path(a.root).expanduser()
    httpd = ThreadingHTTPServer((a.host, a.port), Hub)
    print(f"cortex hub on http://{a.host}:{a.port} · {len(vaults(Hub.root))} vaults under {Hub.root}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
