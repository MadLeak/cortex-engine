# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Keep every cortex on the latest Cortex, checked each time an agent starts a task (`brief`).

1. **This vault, at once.** A vault whose engine is older than the hub's gets the hub's engine
   (scripts/upgrade_engine.py, one commit `engine: v<version>` by the Cortex hub), and a vault whose data is behind its
   engine's schema gets its migrations (`brain.py migrate --commit`). Deterministic, no model. The hub also does this for
   every vault when it starts; on `brief` it catches what came in since: a vault copied in, pulled from another machine,
   or made by an older tool.
2. **A newer Cortex, said once a day.** In the background, at most every CORTEX_UPDATE_HOURS (24), the hub reads the
   engine version on the default branch of CORTEX_UPDATE_REPO (MadLeak/cortex-engine, public: no token) from the first
   4 KB of brain_core.py. When it is newer than the hub's, `brief` tells the agent, with the two commands that apply it;
   the next `brief` after that brings each vault up to it (step 1). The container cannot rebuild itself (it has no access
   to Docker, on purpose), so the owner runs them. CORTEX_UPDATE_CHECK=0 turns this check off.

Never an error for the agent: whatever fails here comes back as one line, or nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
TEMPLATE = ENGINE / "template-vault"
VERSION_LINE = re.compile(r'^VERSION = "([^"]+)"', re.M)
HUB_IDENTITY = {"GIT_AUTHOR_NAME": "Cortex hub", "GIT_AUTHOR_EMAIL": "hub@cortex.local",
                "GIT_COMMITTER_NAME": "Cortex hub", "GIT_COMMITTER_EMAIL": "hub@cortex.local"}
STATE = (".cortex-hub", "update-state.json")   # beside the vaults: the last check, so a restart does not ask again
CORE = "template-vault/_brain/scripts/brain_core.py"


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def file_version(core: Path) -> str:
    try:
        m = VERSION_LINE.search(core.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return m.group(1) if m else ""


def hub_version() -> str:
    """The engine this hub carries: what every vault is brought up to."""
    return file_version(TEMPLATE / "_brain/scripts/brain_core.py")


def current_schema() -> int:
    try:
        return int(json.loads((TEMPLATE / "_brain/config.json").read_text(encoding="utf-8")).get("schema", 0))
    except (OSError, ValueError, TypeError):
        return 0


def vault_schema(v: Path) -> int:
    try:
        return int(json.loads((v / "_brain/config.json").read_text(encoding="utf-8")).get("schema", 0))
    except (OSError, ValueError, TypeError):
        return 0


def git(v: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(v), *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, **HUB_IDENTITY})


# ---- 1. one vault on the hub's engine and schema

def upgrade_one(v: Path, want: str, why: str = "upgraded by the Cortex hub on start") -> str:
    """The vault on engine `want`: '' when it already is, else one line saying what happened. A newer engine is left
    alone. Committed by the Cortex hub when the vault has commits and nothing else is staged."""
    name = v.name
    have = file_version(v / "_brain/scripts/brain_core.py") or "?"
    if version_tuple(have) >= version_tuple(want):
        return f"{name}: engine v{have} is newer than the hub's v{want}: left alone" \
            if version_tuple(have) > version_tuple(want) else ""
    r = subprocess.run([sys.executable, str(ENGINE / "scripts/upgrade_engine.py"), str(v)],
                       capture_output=True, text=True, timeout=300)
    if "update" not in r.stdout:
        return f"{name}: upgrade from v{have} failed: {(r.stderr or r.stdout).strip()[-300:]}"
    if git(v, "rev-parse", "-q", "--verify", "HEAD").returncode != 0:
        return f"{name}: engine v{have} -> v{want} (no first commit yet: left for its owner to commit)"
    if git(v, "diff", "--cached", "--name-only").stdout.strip():
        return f"{name}: engine v{have} -> v{want} (something else is staged: left uncommitted)"
    git(v, "add", "--", "_brain/scripts", ".githooks")
    c = git(v, "commit", "-q", "-m", f"engine: v{want}", "-m", why)
    return f"{name}: engine v{have} -> v{want}" + ("" if c.returncode == 0 else
                                                  f" (the commit was refused: {(c.stderr or c.stdout).strip()[-200:]})")


def migrate_one(v: Path) -> list[str]:
    """The vault's data up to its engine's schema (engine 2.17+): `brain.py migrate --commit`, one commit per migration
    by the Cortex hub. A migration that cannot run waits and says why."""
    if version_tuple(file_version(v / "_brain/scripts/brain_core.py")) < (2, 17, 0):
        return []
    r = subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "--vault", str(v), "migrate", "--commit"],
                       capture_output=True, text=True, timeout=600, env={**os.environ, **HUB_IDENTITY})
    return [f"{v.name}: {x.strip()}" for x in (r.stdout + r.stderr).splitlines() if x.strip() and not x.startswith("schema ")]


def bring_current(v: Path) -> list[str]:
    """For `brief`: the vault on the hub's engine and schema. Two small reads when it already is (the common case)."""
    want = hub_version()
    if not want or not file_version(v / "_brain/scripts/brain_core.py"):   # no engine we can read: not ours to touch
        return []
    out = []
    line = upgrade_one(v, want, "upgraded by the Cortex hub when an agent started a task")
    if line and "left alone" not in line:
        out.append(line)
    if vault_schema(v) < current_schema():
        out += migrate_one(v)
    return out


# ---- 2. a newer Cortex published upstream

class Upstream:
    """The latest engine version on the default branch of the repository, asked at most once per interval, in a
    background thread so that `brief` never waits on the network. One per process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = ""        # the version read last, '' before the first answer
        self.checked = 0.0      # when (time.time()), success or not
        self.error = ""
        self.running = False
        self.loaded = False

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("CORTEX_UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")

    @staticmethod
    def interval() -> float:
        try:
            return max(1.0, float(os.environ.get("CORTEX_UPDATE_HOURS", "24"))) * 3600
        except ValueError:
            return 24 * 3600

    @staticmethod
    def url() -> str:
        repo = os.environ.get("CORTEX_UPDATE_REPO", "MadLeak/cortex-engine").strip().strip("/")
        branch = os.environ.get("CORTEX_UPDATE_BRANCH", "main").strip() or "main"
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{CORE}"

    def fetch(self) -> str:
        req = urllib.request.Request(self.url(), headers={"Range": "bytes=0-4095", "User-Agent": "cortex-hub"})
        with urllib.request.urlopen(req, timeout=10) as r:   # noqa: S310 - a fixed https address
            head = r.read(8192).decode("utf-8", errors="replace")
        m = VERSION_LINE.search(head)
        if not m:
            raise ValueError("no VERSION line in the first 4 KB")
        return m.group(1)

    def load(self, root: Path) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            st = json.loads(root.joinpath(*STATE).read_text(encoding="utf-8"))
            self.latest, self.checked, self.error = str(st.get("latest", "")), float(st.get("checked", 0)), str(st.get("error", ""))
        except (OSError, ValueError, TypeError):
            pass

    def save(self, root: Path) -> None:
        d = root / STATE[0]
        if not d.is_dir():   # only where the hub keeps its data; a host-run MCP server keeps it in memory
            return
        try:
            tmp = d / (STATE[1] + ".tmp")
            tmp.write_text(json.dumps({"latest": self.latest, "checked": self.checked, "error": self.error}), encoding="utf-8")
            tmp.replace(d / STATE[1])
        except OSError:
            pass

    def run(self, root: Path) -> None:
        try:
            latest, error = self.fetch(), ""
        except Exception as e:   # noqa: BLE001 - offline, blocked, renamed: try again next interval
            latest, error = "", f"{type(e).__name__}: {e}"[:200]
        with self.lock:
            self.latest = latest or self.latest
            self.error = error
            self.checked = time.time()
            self.running = False
            self.save(root)

    def poke(self, root: Path) -> None:
        """Start a check when the last one is older than the interval (none at all: now)."""
        if not self.enabled():
            return
        with self.lock:
            self.load(root)
            if self.running or time.time() - self.checked < self.interval():
                return
            self.running = True
        threading.Thread(target=self.run, args=(root,), daemon=True, name="cortex-update-check").start()

    def notice(self) -> str:
        """One line for `brief` when a newer Cortex is published, else ''."""
        have = hub_version()
        with self.lock:
            latest = self.latest
        if not latest or not have or version_tuple(latest) <= version_tuple(have):
            return ""
        return (f"update available: Cortex v{latest} is published; this hub runs v{have}. Tell the owner to apply it: in "
                "the Cortex engine folder run `git pull` and then `docker compose up -d --build`. Every cortex is brought "
                "up to it on its next brief; nothing in it is lost.")


UPSTREAM = Upstream()


def on_brief(v: Path) -> str:
    """Everything `brief` adds about updates, as one section body, or '' when there is nothing to say."""
    try:
        lines = bring_current(v)
    except Exception as e:   # noqa: BLE001
        lines = [f"{v.name}: could not be brought up to date: {e}"[:300]]
    try:
        UPSTREAM.poke(v.parent)
        note = UPSTREAM.notice()
    except Exception:   # noqa: BLE001
        note = ""
    return "\n".join(lines + ([note] if note else []))
