# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""REM dreaming inside the hub container (engine 2.22.0): the questions each note answers (doc2query), written by the
local model server (Strata, `CORTEX_STRATA`), on a schedule or on demand, one cortex at a time.

    enabled per cortex (`rem_enabled` in _brain/config.json, off by default) · every `rem_interval_minutes` · for at
    most `rem_budget_minutes` per run · hottest notes first · Settings has a "Dream now" button, and
    `./cortex.sh rem <vault>` does the same from the command line.

What a run does, in order, and nothing else:
  1. refuses unless the owner's identity is set (CORTEX_GIT_NAME / CORTEX_GIT_EMAIL): what it writes is committed as
     the owner, never as the hub or an agent, and it never starts the model server (when it does not answer, the
     run is skipped and tried again at the next interval);
  2. lists the notes with no questions yet or stale ones, with the vault's own engine;
  3. asks the model for them in small batches and tells the panel after each batch (`brain.py activity dreamed`, the
     violet twinkles the panel already draws);
  4. hands the answers to the vault's own `brain.py questions import` (which checks ids and drops credential-like
     text) and commits `_brain/questions.jsonl` as the owner, as `rem: ...`, when nothing else is staged.

Model use is the one place the hub calls a model, and only here, only when the owner turned it on. Migrations, upgrades
and imports stay plain code."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import hub_settings

QUESTIONS = ("_brain", "questions.jsonl")
BATCH = 3                      # notes per model call
TICK = 60.0                    # seconds between looks at the schedule
LIST_CHILD = r'''
import json, re, sys
sys.path.insert(0, sys.argv[1] + "/_brain/scripts")
import brain_core as bc
v = bc.Vault(sys.argv[1]); w = bc.Telemetry(v).weights(); qs = bc.questions(v)
out = []
for n in v.net.values():
    if n.layer not in (1, 2, 3) or n.status == "obsolete":
        continue
    if n.key in qs and qs[n.key].get("hash") == bc.content_hash(n):
        continue
    text = re.sub(r"\s+", " ", bc.own_text(n.body)).strip()[:1100]
    out.append({"id": n.key, "title": n.title, "summary": n.summary, "text": text, "w": w.get(n.key, 0.0)})
print(json.dumps(out, ensure_ascii=False))
'''


def lifecycle():
    """The model-calling helpers of lifecycle.py (the host service shares them), imported only when a run needs them."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import lifecycle as lc
    return lc


def settings(v: Path) -> dict:
    cfg = hub_settings.read_config(v)
    return {k: cfg.get(k, hub_settings.DEFAULTS[k]) for k in ("rem_enabled", "rem_interval_minutes", "rem_budget_minutes")}


def state_path(root: Path) -> Path:
    return root / ".cortex-hub" / "rem-state.json"


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


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def engine(v: Path, *args: str, stdin: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "--vault", str(v), *args], cwd=str(v.parent),
                          input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def busy(v: Path) -> bool:
    """A git operation in flight: leave the vault alone this round."""
    g = v / ".git"
    return any((g / x).exists() for x in ("index.lock", "MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def commit_questions(v: Path, count: int) -> str:
    """Commits _brain/questions.jsonl as the owner (and only as the owner)."""
    env = hub_settings.owner_env()
    rel = "/".join(QUESTIONS)
    if env is None:
        return "not committed: set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL"
    if hub_settings.git(v, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return "not committed: not a git repository"
    if hub_settings.git(v, "diff", "--cached", "--name-only").stdout.strip():
        return "not committed: something else is staged"
    hub_settings.git(v, "add", "--", rel)
    r = hub_settings.git(v, "commit", "-q", "-m", f"rem: questions for {count} notes", env=env, timeout=300)
    return "committed as the owner" if r.returncode == 0 else f"not committed: {(r.stderr or r.stdout).strip()[-200:]}"


def dream(v: Path, minutes: float, strata: str | None = None, key: Path | None = None) -> dict:
    """One run on one cortex. Returns {"t", "result", "done", "left"}; never raises for an expected refusal."""
    out = {"t": now(), "done": 0, "left": 0}
    lc = lifecycle()
    url = strata or lc.STRATA
    if hub_settings.owner_env() is None:
        return {**out, "result": "skipped: set CORTEX_GIT_NAME and CORTEX_GIT_EMAIL (what REM writes is committed as the owner)"}
    if busy(v):
        return {**out, "result": "skipped: git is busy in this cortex"}
    if not lc.strata_up(url):
        return {**out, "result": f"skipped: the model server does not answer at {url}"}
    p = subprocess.run([sys.executable, "-I", "-c", LIST_CHILD, str(v)], capture_output=True, text=True, timeout=600)
    if p.returncode:
        return {**out, "result": "skipped: this cortex's engine cannot list its questions (needs engine 2.7.0 or newer)"}
    todo = sorted(json.loads(p.stdout), key=lambda n: -n["w"])
    got, deadline, failed = {}, time.time() + minutes * 60, 0
    for i in range(0, len(todo), BATCH):
        if time.time() >= deadline:
            break
        batch = todo[i:i + BATCH]
        try:
            answers = lc.strata_questions(batch, url=url, key_file=key)
        except (OSError, ValueError, KeyError) as e:
            out["error"] = f"model call failed ({e}); stopped"
            break
        failed += sum(1 for n in batch if n["id"] not in answers)
        got.update(answers)
        done = [n["id"] for n in batch if n["id"] in answers]
        if done:
            engine(v, "activity", "dreamed", "--nodes", "-", "--note", f"REM {len(got)} notes", stdin="\n".join(done), timeout=60)
    if got:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as f:
            f.write("".join(json.dumps({"id": k, **x}, ensure_ascii=False) + "\n" for k, x in got.items()))
        try:
            imp = engine(v, "questions", "import", f.name)
        finally:
            os.unlink(f.name)
        if imp.returncode:
            return {**out, "result": f"the engine refused the questions: {(imp.stdout + imp.stderr).strip()[-300:]}"}
        out["commit"] = commit_questions(v, len(got))
    out.update(done=len(got), left=len(todo) - len(got))
    out["result"] = (f"dreamed {len(got)} notes" + (f", {failed} unparsed" if failed else "") + f", {out['left']} left"
                     + (f" ({out['error']})" if "error" in out else "") + (f"; {out['commit']}" if "commit" in out else ""))
    return out


class Scheduler:
    """Runs the cortexes whose REM is on, when their interval is up, one run at a time; also on demand."""

    def __init__(self, root: Path, listing, runner=dream):
        self.root, self.listing, self.runner = root, listing, runner   # listing() -> {folder: Path}
        self.lock = threading.Lock()
        self.running: set[str] = set()

    def due(self, v: Path, last: str | None) -> bool:
        s = settings(v)
        if s["rem_enabled"] is not True:
            return False
        if not last:
            return True
        try:
            return (datetime.now().astimezone() - datetime.fromisoformat(last)).total_seconds() >= s["rem_interval_minutes"] * 60
        except ValueError:
            return True

    def run(self, v: Path, manual: bool = False) -> dict | None:
        """Dreams one cortex now. None when it is already dreaming."""
        with self.lock:
            if v.name in self.running:
                return None
            self.running.add(v.name)
        try:
            res = self.runner(v, float(settings(v)["rem_budget_minutes"]))
        except Exception as e:   # noqa: BLE001 — a failed run is a line in the status, never the end of the scheduler
            res = {"t": now(), "done": 0, "left": 0, "result": f"failed: {e}"}
        finally:
            with self.lock:
                self.running.discard(v.name)
        res["manual"] = manual
        st = read_state(self.root)
        st[v.name] = res
        write_state(self.root, st)
        return res

    def start_now(self, v: Path) -> bool:
        """On demand, in the background: False when this cortex is already dreaming."""
        with self.lock:
            if v.name in self.running:
                return False
        threading.Thread(target=self.run, args=(v, True), daemon=True, name=f"rem-{v.name}").start()
        time.sleep(0.05)
        return True

    def tick(self) -> None:
        for name, v in self.listing().items():
            with self.lock:
                if name in self.running:
                    continue
            if self.due(v, read_state(self.root).get(name, {}).get("t")):
                self.run(v)

    def loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:   # noqa: BLE001
                print(f"rem scheduler: {e}", file=sys.stderr, flush=True)
            time.sleep(TICK)

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True, name="rem-scheduler").start()

    def status(self, v: Path) -> dict:
        s = settings(v)
        last = read_state(self.root).get(v.name) or {}
        return {"enabled": s["rem_enabled"] is True, "interval": s["rem_interval_minutes"], "running": v.name in self.running,
                "last": last.get("t"), "result": last.get("result"), "model": lifecycle().STRATA}
