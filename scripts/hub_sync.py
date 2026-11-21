# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Keep a cortex in step with its remote: it starts from the latest and never holds a change only on this machine.

One cycle, for a cortex that turned sync on in Settings (`sync_enabled`):
  1. what changed here is committed as the owner (CORTEX_GIT_NAME / CORTEX_GIT_EMAIL), through the vault's own
     pre-commit hook, so the lint still guards every commit;
  2. the remote is fetched; a remote that moved is fast-forwarded, or the local commits are rebased on it;
  3. what is ahead is pushed (https only, the token in one HTTP header, the vault's pre-push hook refuses a public repo).
Nothing is ever forced. A rebase that conflicts is aborted and reported in Settings: the owner decides.
The token is the vault's own (Settings) or one shared file, <vaults>/.cortex-hub/push.token.
Deterministic: no model, no network except the remote.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import hub_settings

TICK = 60.0                  # seconds between looks at the schedule
RETRY_MINUTES = 2            # a failed cycle is tried again this soon, not after the whole interval
REQUIRE_HTTPS = True         # tests turn it off to use a local bare repository as the remote
GLOBAL_TOKEN = (".cortex-hub", "push.token")
EXCLUDE = (":(exclude).claude",)   # an agent's own folder inside the vault is never a note


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def settings(v: Path) -> dict:
    cfg = hub_settings.read_config(v)
    return {k: cfg.get(k, hub_settings.DEFAULTS[k]) for k in ("sync_enabled", "sync_interval_minutes", "sync_idle_minutes")}


def state_path(root: Path) -> Path:
    return root / ".cortex-hub" / "sync-state.json"


def read_state(root: Path) -> dict:
    try:
        return json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(root: Path, state: dict) -> None:
    p = state_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def busy(v: Path) -> bool:
    """A git operation in flight: leave the vault alone this round."""
    g = v / ".git"
    return any((g / x).exists() for x in ("index.lock", "MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def token(v: Path) -> str:
    """The vault's own push token, else the shared one (hub_settings.shared_token); '' when there is none."""
    try:
        t = hub_settings.push_token_path(v).read_text(encoding="utf-8").strip()
    except OSError:
        t = ""
    return t or hub_settings.shared_token(v)


def run(v: Path, args: list[str], secret: str = "", timeout: int = 120, env: dict | None = None) -> tuple[int, str]:
    """git in the vault, with the token (if any) in one header and scrubbed from what comes back."""
    pre: list[str] = []
    basic = ""
    if secret:
        basic = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        pre = ["-c", "credential.helper=", "-c", f"http.extraHeader=Authorization: Basic {basic}"]
    r = hub_settings.git(v, *pre, *args, env=env or {**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=timeout)
    text = (r.stdout + "\n" + r.stderr).strip()
    for s in (secret, basic):
        if s:
            text = text.replace(s, "<token>")
    return r.returncode, text


def _commit_local(v: Path) -> tuple[bool, str]:
    """Commit what changed here as the owner. (committed, problem): a problem stops the cycle before it pulls."""
    if not hub_settings.git(v, "status", "--porcelain").stdout.strip():
        return False, ""
    env = hub_settings.owner_env()
    if env is None:
        return False, "not synced: set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL"
    if hub_settings.git(v, "diff", "--cached", "--name-only").stdout.strip():
        return False, "not synced: something else is staged"
    hub_settings.git(v, "add", "-A", "--", ".", *EXCLUDE)
    n = len(hub_settings.git(v, "diff", "--cached", "--name-only").stdout.split())
    if not n:
        return False, ""
    r = hub_settings.git(v, "commit", "-q", "-m", f"sync: {n} change{'s' if n != 1 else ''} saved by the hub", env=env, timeout=300)
    if r.returncode:
        hub_settings.git(v, "reset", "-q")   # the hook refused it (lint): leave nothing staged, report why
        return False, f"commit refused: {(r.stderr or r.stdout).strip()[-200:]}"
    return True, ""


def cycle(v: Path, push: bool = True) -> dict:
    """One sync of one cortex. Always returns a result line; never raises, never forces."""
    res = {"t": now(), "committed": False, "pulled": 0, "pushed": 0, "result": "ok"}
    try:
        if busy(v):
            res["result"] = "skipped: a git operation is in flight"
            return res
        remote = hub_settings.git(v, "remote", "get-url", "origin").stdout.strip()
        if not remote:
            res["result"] = "not synced: no remote set"
            return res
        if REQUIRE_HTTPS and not remote.startswith("https://"):
            res["result"] = "not synced: the hub syncs over https only"
            return res
        secret = token(v) if remote.startswith("https://") else ""
        if REQUIRE_HTTPS and not secret:
            res["result"] = "not synced: needs a push token (Settings, or <vaults>/.cortex-hub/push.token)"
            return res
        res["committed"], problem = _commit_local(v)
        if problem:
            res["result"] = problem
            return res
        branch = hub_settings.git(v, "branch", "--show-current").stdout.strip()
        if not branch:
            res["result"] = "not synced: detached HEAD"
            return res
        code, text = run(v, ["fetch", "-q", "origin"], secret)
        if code:
            res["result"] = f"fetch failed: {text[-200:]}"
            return res
        up = f"origin/{branch}"
        if hub_settings.git(v, "rev-parse", "-q", "--verify", up).returncode == 0:
            ahead, behind = (int(x) for x in hub_settings.git(v, "rev-list", "--left-right", "--count", f"HEAD...{up}").stdout.split())
            if behind and not ahead:
                code, text = run(v, ["merge", "--ff-only", "-q", up])
            elif behind:
                code, text = run(v, ["rebase", up], env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(hub_settings.owner_env() or {})})
                if code:
                    run(v, ["rebase", "--abort"])
                    res["result"] = f"diverged: this cortex and the remote both changed, left for you ({text[-160:]})"
                    return res
            else:
                code, text = 0, ""
            if code:
                res["result"] = f"pull failed: {text[-200:]}"
                return res
            res["pulled"] = behind
        if push:
            out = hub_settings.git(v, "rev-list", "--count", f"{up}..HEAD") if hub_settings.git(v, "rev-parse", "-q", "--verify", up).returncode == 0 else None
            to_push = int(out.stdout.strip() or 0) if out is not None else 1
            if to_push:
                code, text = run(v, ["push", "-q", "-u", "origin", "HEAD"], secret, timeout=600)
                if code:
                    res["result"] = f"push failed: {text[-200:]}"
                    return res
                res["pushed"] = to_push
        return res
    except Exception as e:   # noqa: BLE001 — a failed cycle is a line in Settings, never the end of the hub
        res["result"] = f"failed: {e}"
        return res


def stale(v: Path) -> bool:
    """True when sync is on and the last cycle is older than `sync_idle_minutes`: a task starting now should first pull."""
    s = settings(v)
    if s["sync_enabled"] is not True:
        return False
    last = read_state(v.parent).get(v.name, {}).get("t")
    if not last:
        return True
    try:
        return (datetime.now().astimezone() - datetime.fromisoformat(last)).total_seconds() >= s["sync_idle_minutes"] * 60
    except ValueError:
        return True


def pull_if_stale(v: Path) -> str:
    """For `brief`: a cortex coming back after a while is brought up to date first. Pull only, never a push, and never
    an error to the caller: '' when nothing was needed, else a short line."""
    try:
        if not stale(v):
            return ""
        res = cycle(v, push=False)
        st = read_state(v.parent)
        st[v.name] = res
        write_state(v.parent, st)
        return f"sync: pulled {res['pulled']}" if res["result"] == "ok" else f"sync: {res['result']}"
    except Exception as e:   # noqa: BLE001
        return f"sync: failed: {e}"


class Scheduler:
    """Syncs the cortexes that turned it on, when their interval is up, one at a time; also on demand."""

    def __init__(self, root: Path, listing, runner=cycle):
        self.root, self.listing, self.runner = root, listing, runner   # listing() -> {folder: Path}
        self.lock = threading.Lock()
        self.running: set[str] = set()

    def due(self, v: Path, last: dict | None) -> bool:
        s = settings(v)
        if s["sync_enabled"] is not True:
            return False
        if not last or not last.get("t"):
            return True
        wait = s["sync_interval_minutes"] if last.get("result") == "ok" else min(RETRY_MINUTES, s["sync_interval_minutes"])
        try:
            return (datetime.now().astimezone() - datetime.fromisoformat(last["t"])).total_seconds() >= wait * 60
        except ValueError:
            return True

    def run(self, v: Path, manual: bool = False, reserved: bool = False) -> dict | None:
        """Syncs one cortex now. None when it is already syncing. `reserved`: the caller already marked it running."""
        with self.lock:
            if not reserved:
                if v.name in self.running:
                    return None
                self.running.add(v.name)
        try:
            res = self.runner(v)
            res["manual"] = manual
            st = read_state(self.root)
            st[v.name] = res
            write_state(self.root, st)
            return res
        finally:
            with self.lock:
                self.running.discard(v.name)

    def start_now(self, v: Path) -> bool:
        with self.lock:
            if v.name in self.running:
                return False
            self.running.add(v.name)
        threading.Thread(target=self.run, args=(v, True, True), daemon=True, name=f"sync-{v.name}").start()
        return True

    def tick(self) -> None:
        for name, v in self.listing().items():
            with self.lock:
                if name in self.running:
                    continue
            if self.due(v, read_state(self.root).get(name)):
                self.run(v)

    def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:   # noqa: BLE001
                print(f"sync scheduler: {e}", file=sys.stderr, flush=True)
            time.sleep(TICK)

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True, name="sync-scheduler").start()

    def status(self, v: Path) -> dict:
        s = settings(v)
        last = read_state(self.root).get(v.name) or {}
        return {"enabled": s["sync_enabled"] is True, "interval": s["sync_interval_minutes"], "idle": s["sync_idle_minutes"],
                "running": v.name in self.running, "last": last.get("t"), "result": last.get("result"),
                "committed": last.get("committed"), "pulled": last.get("pulled"), "pushed": last.get("pushed"),
                "token": bool(token(v))}
