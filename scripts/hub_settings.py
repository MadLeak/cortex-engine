# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""A cortex's settings, as the hub's Settings dialog reads and changes them: on this machine only, one cortex at a
time, every change explicit.

- the config keys the engine reads (name, learner, idle minutes, weight half-life, confirmation rules, provisional cap),
  written to _brain/config.json and committed as the owner;
- the git remote's URL (only the vault's own git config: nothing on the forge changes);
- a push token for that remote, kept in <vault>/_brain/state/remote.token (out of git, never sent back to a browser),
  and "Save to remote", the one place a vault is ever pushed, and only when the owner asks;
- the MCP token, rotated (the old one stops working at once);
- delete: the whole folder, for good, only with the cortex's exact name typed in.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PUSH_TOKEN = ("_brain", "state", "remote.token")
# key: (kind, low, high, what it does) - the keys the engine reads from _brain/config.json, with its own defaults
CONFIG = {
    "vault": ("text", 1, 48, "the cortex's name, as panels and the agent see it (its folder stays)"),
    "learner": ("slug", 1, 32, "the agent that learns here: its learnings and commits carry this name"),
    "auto_idle_minutes": ("int", 5, 1440, "minutes without a call before a fallback session closes"),
    "half_life_days": ("int", 1, 3650, "days for an unused node's weight to halve"),
    "confirm_sessions": ("int", 1, 20, "sessions in which a provisional learning must help before it becomes a seed"),
    "confirm_min_days": ("int", 0, 60, "days those sessions must span at least"),
    "max_provisional": ("int", 1, 500, "provisional learnings allowed to wait; past it, learnings go to _inbox"),
    "ideas_min_count": ("int", 2, 50, "nodes a word must recur in before the ideas miner proposes a concept for it"),
    "rem_enabled": ("bool", 0, 1, "dream the questions each note answers on a schedule (needs the local model server)"),
    "rem_interval_minutes": ("int", 15, 10080, "minutes between two dreaming runs"),
    "rem_budget_minutes": ("int", 1, 240, "most minutes one dreaming run may take"),
}
DEFAULTS = {"auto_idle_minutes": 30, "half_life_days": 30, "confirm_sessions": 3, "confirm_min_days": 2,
            "max_provisional": 25, "ideas_min_count": 4, "rem_enabled": False, "rem_interval_minutes": 1440,
            "rem_budget_minutes": 30}
REMOTE = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?/[\w.~/-]+?(\.git)?|git@[A-Za-z0-9.-]+:[\w.~/-]+?(\.git)?)$")


def git(v: Path, *args: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(v), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def owner_env() -> dict | None:
    name, email = os.environ.get("CORTEX_GIT_NAME", ""), os.environ.get("CORTEX_GIT_EMAIL", "")
    if not name or not email:
        return None
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def read_config(v: Path) -> dict:
    try:
        return json.loads((v / "_brain" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def push_token_path(v: Path) -> Path:
    return v.joinpath(*PUSH_TOKEN)


def read(v: Path, scope: str) -> dict:
    """What the dialog shows. Secrets are never sent: only whether a push token is set."""
    cfg = read_config(v)
    config = {k: cfg.get(k, DEFAULTS.get(k, "")) for k in CONFIG}
    config["vault"] = config["vault"] or v.name
    in_git = git(v, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    remote = git(v, "remote", "get-url", "origin").stdout.strip() if in_git else ""
    out = {"folder": v.name, "config": config, "help": {k: d[3] for k, d in CONFIG.items()}, "scope": scope,
           "schema": cfg.get("schema", 0), "layers": cfg.get("layers", 3), "ideas_waiting": ideas_waiting(v), "git": in_git, "remote": remote,
           "push_token": push_token_path(v).is_file() and bool(push_token_path(v).read_text(encoding="utf-8").strip())}
    if in_git:
        out["branch"] = git(v, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        out["uncommitted"] = len([x for x in git(v, "status", "--porcelain").stdout.splitlines() if x.strip()])
        out["last_commit"] = git(v, "log", "-1", "--format=%h %an · %s · %cr").stdout.strip()
        up = git(v, "rev-list", "--count", "@{u}..HEAD")
        out["unpushed"] = int(up.stdout.strip()) if up.returncode == 0 else None   # None: never pushed / no upstream
        out["commits"] = int(git(v, "rev-list", "--count", "HEAD").stdout.strip() or 0)
    return out


def save(v: Path, changes: dict) -> str:
    """Validates and writes the config keys that changed, then commits _brain/config.json as the owner (when it had
    no uncommitted edits of its own and nothing else is staged). ValueError = a bad value."""
    cfg = read_config(v)
    changed = {}
    for k, val in changes.items():
        if k not in CONFIG:
            raise ValueError(f"unknown setting {k}")
        kind, low, high, _ = CONFIG[k]
        if kind == "bool":
            if val not in (True, False, "true", "false", "on", "off"):
                raise ValueError(f"{k}: on or off")
            val = val in (True, "true", "on")
        elif kind == "int":
            try:
                val = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"{k}: a whole number") from None
            if not low <= val <= high:
                raise ValueError(f"{k}: between {low} and {high}")
        else:
            val = str(val).strip()
            if not low <= len(val) <= high:
                raise ValueError(f"{k}: {low} to {high} characters")
            if kind == "slug" and not re.fullmatch(r"[\w-]+", val):
                raise ValueError(f"{k}: letters, digits, dashes or underscores")
            if kind == "text" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", val):
                raise ValueError(f"{k}: letters, digits, spaces, dots, dashes or underscores")
        if cfg.get(k, DEFAULTS.get(k)) != val:
            changed[k] = val
    if not changed:
        return "nothing changed"
    path = v / "_brain" / "config.json"
    dirty = git(v, "status", "--porcelain", "--", "_brain/config.json").stdout.strip()
    cfg.update(changed)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    what = ", ".join(f"{k} = {x}" for k, x in changed.items())
    env = owner_env()
    if dirty or env is None or git(v, "diff", "--cached", "--name-only").stdout.strip():
        return f"saved {what} (not committed: " + ("config.json already had edits of its own)" if dirty else
                                                   "set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL)" if env is None else
                                                   "something else is staged)")
    git(v, "add", "--", "_brain/config.json")
    r = git(v, "commit", "-q", "-m", f"config: {what}", env=env, timeout=300)
    return f"saved {what}" + ("" if r.returncode == 0 else f" (not committed: {(r.stderr or r.stdout).strip()[-200:]})")


def commit_paths(v: Path, paths: list[str], message: str) -> str:
    """Commits these paths as the owner, and only as the owner (CORTEX_GIT_NAME / CORTEX_GIT_EMAIL), and only when
    nothing else is staged: what the hub writes on its own initiative never carries another name."""
    env = owner_env()
    if env is None:
        return "not committed: set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL"
    if git(v, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return "not committed: not a git repository"
    if git(v, "diff", "--cached", "--name-only").stdout.strip():
        return "not committed: something else is staged"
    here = [p for p in paths if (v / p).exists()]
    if not here:
        return "nothing to commit"
    git(v, "add", "--", *here)
    r = git(v, "commit", "-q", "-m", message, env=env, timeout=300)
    return "committed as the owner" if r.returncode == 0 else f"not committed: {(r.stderr or r.stdout).strip()[-200:]}"


def ideas_waiting(v: Path) -> int:
    """Ideas from the miner still waiting in _inbox for the curator."""
    n = 0
    inbox = v / "_inbox"
    for f in inbox.glob("*.md") if inbox.is_dir() else []:
        try:
            if '\nidea: "' in f.read_text(encoding="utf-8", errors="replace")[:600]:
                n += 1
        except OSError:
            continue
    return n


def find_ideas(v: Path) -> str:
    """The ideas miner (`brain.py ideas`): notes in _inbox for the curator, never in the graph; committed as the owner.
    Deterministic: no model. Refuses without the owner's identity, like every commit the hub makes for you."""
    if owner_env() is None:
        raise ValueError("set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL: what the miner writes is committed as you")
    cfg = read_config(v)
    r = subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "--vault", str(v), "ideas", "--min-count",
                        str(cfg.get("ideas_min_count", DEFAULTS["ideas_min_count"]))], cwd=str(v.parent),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    text = (r.stdout + r.stderr).strip()
    if r.returncode:
        raise RuntimeError(f"the ideas miner failed (needs engine 2.23.0 or newer):\n{text[-600:]}")
    first = text.splitlines()[0] if text else "ideas: nothing"
    if " 0 written" in first:
        return first
    return f"{first}; {commit_paths(v, ['_inbox', '_log/ideas.jsonl'], 'ideas: candidates for the curator')}"


def set_remote(v: Path, url: str) -> str:
    """The vault's own git remote: set, changed or removed. Nothing on the forge changes."""
    url = (url or "").strip()
    if git(v, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise ValueError("this cortex is not a git repository")
    have = git(v, "remote", "get-url", "origin").stdout.strip()
    if not url:
        if have:
            git(v, "remote", "remove", "origin")
        return "remote removed (only from this vault's git config)"
    if not REMOTE.fullmatch(url) or (url.startswith("https://") and "@" in url[8:].split("/", 1)[0]):
        raise ValueError("remote: https://host/owner/repo.git or git@host:owner/repo.git, with no credentials in it")
    git(v, "remote", "set-url" if have else "add", "origin", url)
    return f"remote set to {url}"


def set_push_token(v: Path, token: str) -> str:
    """The push token for this vault's remote, kept in _brain/state/remote.token (out of git)."""
    token = (token or "").strip()
    p = push_token_path(v)
    if not token:
        p.unlink(missing_ok=True)
        return "push token removed"
    if len(token) > 512 or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise ValueError("push token: one line of printable characters")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return "push token saved (in this vault's _brain/state/remote.token, never committed)"


def push(v: Path) -> str:
    """Save to remote: `git push` of the current branch to origin, with the vault's push token, and the vault's own
    pre-push hook (which refuses a public repository). The token travels in one HTTP header, never in a URL or a
    config file; any trace of it is scrubbed from the answer."""
    remote = git(v, "remote", "get-url", "origin").stdout.strip()
    if not remote:
        raise ValueError("set a remote first")
    if not remote.startswith("https://"):
        raise ValueError("the hub pushes over https only (ssh keys stay on your machine): use an https remote")
    p = push_token_path(v)
    token = p.read_text(encoding="utf-8").strip() if p.is_file() else ""
    if not token:
        raise ValueError("set a push token first")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    r = git(v, "-c", "credential.helper=", "-c", f"http.extraHeader=Authorization: Basic {basic}",
            "push", "-u", "origin", "HEAD", env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=600)
    text = (r.stdout + "\n" + r.stderr).replace(token, "<token>").replace(basic, "<token>").strip()
    if r.returncode:
        raise RuntimeError(f"push failed:\n{text[-1500:]}")
    return "saved to remote\n" + text[-800:]


def delete(v: Path, typed: str) -> str:
    """Deletes the cortex for good: its whole folder (notes, history, tokens). Only with its exact folder name typed."""
    if typed != v.name:
        raise ValueError(f'type the cortex\'s exact name, "{v.name}", to delete it')

    def unlock(func, path, _exc):   # git keeps its objects read-only
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)

    shutil.rmtree(v, onexc=unlock) if "onexc" in shutil.rmtree.__code__.co_varnames else shutil.rmtree(v, onerror=unlock)
    return f"{v.name} deleted"
