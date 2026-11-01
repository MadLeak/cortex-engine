#!/usr/bin/env python3
"""Note renewal: the life-cycle phase that keeps current notes true. A note ages by its type and its use; when it is
due, an agent checks it against the live system (read-only) and the script, not the agent, applies the verdict.

    python3 scripts/renewal.py due VAULT [--as-of 2026-12-15] [--limit 20]
    python3 scripts/renewal.py renew VAULT --limit 10 [--as-of DATE] [--agent hermes] [--reasoning medium]

Before any model runs, the script itself checks what a note names that code can check (paths, config keys), and the
agent gets those results, so it never has to prove an absence; a note with nothing checkable is not sent to a model
(needs_experiment). While the agent works, its session is watched in Hermes's state.db: a repeated command or too
many calls stops it (loop). One note at a time: the next starts only once the last session is quiet, since Strata
has a single slot.

Verdicts: still_true (only `reviewed` moves), changed (the main section is rewritten, the note goes back to seed, since
only the owner validates new content), not_applicable (left for the owner, listed in the report), unverifiable (the
system is out of reach from here; nothing changes). Every applied note is one commit; the vault's bench runs after
it and a commit that makes recall worse is reverted, so a renewal can never lower retrieval quality. Anything the agent
itself changed in the vault is stashed, never kept. Results: ~/.local/share/cortex-life/renewal/<vault>.jsonl.
"""
import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import sqlite3
import tempfile
import shutil
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifecycle  # noqa: E402  (BENCH_CHILD, the same bench the night phase runs)

DATA = Path(os.environ.get("CORTEX_LIFE_DATA", Path.home() / ".local/share/cortex-life")) / "renewal"
TTL_DAYS = {"fact": 30, "error": 60, "concept": 90, "procedure": 90, "decision": 180, "example": 180}
HOT = 10            # reads + 3 x credits at which a note ages twice as fast
TIMEOUT = 600       # seconds per note; past it the note is skipped
BENCH_DROP_HIT1 = 1
BENCH_DROP_MRR = 0.02
VERDICTS = ("still_true", "changed", "not_applicable", "unverifiable")
MAX_TURNS = 10      # the agent's own cap (hermes chat --max-turns)
MAX_CALLS = 12      # tool calls before the watcher stops a session
MAX_REPEAT = 3      # the same command this many times is a loop
QUIET_S = 20        # a session silent this long has let go of Strata's slot
HERMES_DB = Path.home() / ".hermes/state.db"
CONFIGS = {"Hermes Cortex": ["~/.hermes/config.yaml", "~/.hermes/profiles/*/config.yaml"]}

PROMPT = """You are checking one note of {vault}, an AI agents' memory, against the live system on this machine
(Alastor, Ubuntu 26.04; Windows is the other boot and is not reachable now). Today is {today}.

Rules: READ-ONLY. You may run commands that only read (cat, ls, grep, --version, systemctl status, ss, curl to
localhost, git log). Never write, edit, move, install, restart or delete anything, and never print secrets: for a file
that may hold a key, check only that a field exists. Use at most 6 commands. If the note is about something you cannot
reach from here (Windows side, another host, an account), do not guess: answer unverifiable.

NOTE: {title}  (L{layer} {type}, truth {truth}, last reviewed {reviewed})
summary: {summary}
{text}

ALREADY CHECKED BY A SCRIPT JUST NOW (trust these; do not search again for something listed as missing):
{facts}

Answer with one JSON object and nothing after it:
{{"verdict": "still_true|changed|not_applicable|unverifiable",
 "evidence": "<the commands you ran and what they showed, at most 200 characters>",
 "new_text": "<only for changed: the corrected text of the note's main section, English, same facts plus the fix, dated>",
 "reason": "<one sentence>"}}
"""


def today(as_of: str | None) -> dt.date:
    return dt.date.fromisoformat(as_of) if as_of else dt.date.today()


def load(vault: Path):
    sys.path.insert(0, str(vault / "_brain/scripts"))
    import brain_core as bc
    return bc.Vault(vault)


def usage(usage_file: Path) -> Counter:
    heat = Counter()
    if usage_file.exists():
        for line in usage_file.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            for k in e.get("keys") or ([e["k"]] if e.get("k") else []):
                heat[k] += 3 if e.get("e") == "used" else 1 if e.get("e") == "read" else 0
    return heat


def ttl(n, heat: Counter) -> int | None:
    """Days a note stays fresh: by its type, halved when agents use it a lot; None when it never ages."""
    if n.layer not in (2, 3) or (n.fm.get("truth") or "") != "current":
        return None
    days = TTL_DAYS.get(n.type or "", 90)
    return days // 2 if heat[n.key] >= HOT else days


def due(vault: Path, as_of: dt.date, usage_file: Path) -> list[dict]:
    v, heat = load(vault), usage(usage_file)
    import brain_core as bc
    host, out = bc.host_system(v) if hasattr(bc, "host_system") else "", []
    for n in v.net.values():
        t = ttl(n, heat)
        if t is None or (host and bc.foreign(n, host)):
            continue   # a note of another system is checked only there: from here it would always look broken
        seen = str(n.fm.get("reviewed") or n.fm.get("updated") or n.fm.get("created") or "")[:10]
        try:
            age = (as_of - dt.date.fromisoformat(seen)).days
        except ValueError:
            age = 10 ** 4
        if age >= t:
            out.append({"node": n, "age": age, "ttl": t, "heat": heat[n.key], "seen": seen})
    out.sort(key=lambda d: (-d["age"] / d["ttl"], -d["heat"]))   # the most overdue first, the most used among equals
    return out


def atoms(text: str) -> tuple[list[str], list[str]]:
    """What a note names that code can check: absolute or home paths, and dotted config keys."""
    code = re.findall(r"`([^`\n]+)`", text)
    paths = {c for c in code if re.fullmatch(r"~?/[\w.\-/]+", c)} | set(re.findall(r"(?<![\w`])(~/[\w.\-/]+|/home/[\w.\-/]+)", text))
    keys = {c for c in code if re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", c) and not re.search(r"\.(py|md|json|yaml|yml|sh|txt|db|env|exe)$", c)}
    paths = {x.rstrip(".,;:)") for x in paths}
    return sorted(x for x in paths if len(x) > 2), sorted(keys)


def lookup(data, key: str):
    for part in key.split("."):
        if not isinstance(data, dict) or part not in data:
            return False, None
        data = data[part]
    return True, data


def precheck(text: str, configs: list[str]) -> tuple[list[str], int]:
    """Checks the note's paths and keys with code; returns the facts and how many things were checkable."""
    import glob
    import yaml
    paths, keys = atoms(text)
    facts = [f"path {p}: {'exists' if Path(p).expanduser().exists() else 'MISSING'}" for p in paths]
    files = sorted({f for c in configs for f in glob.glob(str(Path(c).expanduser()), recursive=True)})
    loaded = {}
    for f in files:
        try:
            loaded[f] = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
    for k in keys:
        hits = [(f, v) for f, d in loaded.items() for ok, v in [lookup(d, k)] if ok]
        if hits:
            facts += [f"key {k} in {f.replace(str(Path.home()), '~')}: {json.dumps(v, default=str)[:120]}" for f, v in hits]
        elif loaded:
            facts.append(f"key {k}: MISSING from {', '.join(f.replace(str(Path.home()), '~') for f in loaded)}")
    return facts, len(paths) + len(keys)


def _value(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    if text.startswith("[") and text.endswith("]"):
        return [x.strip().strip("'\"") for x in text[1:-1].split(",") if x.strip()]
    return text.strip("'\"")


def _same(found, want) -> bool:
    norm = lambda v: sorted(map(str, v)) if isinstance(v, list) else str(v).lower()  # noqa: E731
    return norm(found) == norm(want)


def run_check(line: str) -> tuple[bool | None, str]:
    """One contract line, read-only. True = the note holds, False = it does not, None = the check itself is broken."""
    import glob
    import urllib.request
    import tomllib
    import yaml
    m = re.match(r"(key|nokey)\s+(\S+)\s+in\s+(\S+)(?:\s*=\s*(.+))?$", line)
    if m:
        kind, key, where, want = m.groups()
        files = glob.glob(str(Path(where).expanduser()), recursive=True)
        if not files:
            return None, f"{where}: no such file"
        for f in files:
            try:
                text = Path(f).read_text(encoding="utf-8")
                ok, val = lookup((tomllib.loads(text) if f.endswith(".toml") else yaml.safe_load(text)) or {}, key)
            except (OSError, yaml.YAMLError, tomllib.TOMLDecodeError) as ex:
                return None, f"{f}: {ex}"
            if kind == "nokey" and ok:
                return False, f"{key} present in {f}"
            if kind == "key" and not ok:
                return False, f"{key} missing in {f}"
            if kind == "key" and want is not None and not _same(val, _value(want)):
                return False, f"{key} in {f} is {json.dumps(val, default=str)[:80]}, the note says {want}"
        return True, f"{kind} {key} ok"
    m = re.match(r"(path|nopath)\s+(\S+)$", line)
    if m:
        there = Path(m.group(2)).expanduser().exists()
        return (there if m.group(1) == "path" else not there), f"{m.group(2)} {'exists' if there else 'missing'}"
    m = re.match(r"grep\s+(.+?)\s+in\s+(\S+)$", line)
    if m:
        files = glob.glob(str(Path(m.group(2)).expanduser()), recursive=True)
        if not files:
            return None, f"{m.group(2)}: no such file"
        try:
            rx = re.compile(m.group(1))
        except re.error as ex:
            return None, f"bad regex: {ex}"
        hit = any(rx.search(l) for f in files for l in Path(f).read_text(encoding="utf-8", errors="replace").splitlines())
        return hit, f"/{m.group(1)}/ {'found' if hit else 'not found'} in {m.group(2)}"
    m = re.match(r"url\s+(http://(?:localhost|127\.0\.0\.1)\S*)\s+(\S+)\s*=\s*(.+)$", line)
    if m:
        try:
            with urllib.request.urlopen(m.group(1), timeout=10) as r:
                ok, val = lookup(json.load(r), m.group(2))
        except (OSError, ValueError) as ex:
            return None, f"{m.group(1)}: {ex}"
        return (ok and _same(val, _value(m.group(3)))), f"{m.group(2)} = {val}"
    m = re.match(r"version\s+(\S+)\s+contains\s+(.+)$", line)
    if m:
        out = subprocess.run([m.group(1), "--version"], capture_output=True, text=True, timeout=30)
        return m.group(2).strip() in out.stdout + out.stderr, (out.stdout + out.stderr).strip()[:80]
    m = re.match(r"service\s+(\S+)\s+active$", line)
    if m:
        out = subprocess.run(["systemctl", "--user", "is-active", m.group(1)], capture_output=True, text=True)
        return out.stdout.strip() == "active", f"{m.group(1)} {out.stdout.strip()}"
    return None, f"unknown check form: {line[:60]}"


def session_calls(vault: Path, since: float) -> tuple[list[str], float]:
    """Tool-call arguments of the agent's sessions in this vault since a time, and the time of the last message."""
    try:
        c = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True, timeout=5)
        ids = [r[0] for r in c.execute("select id from sessions where cwd = ? and started_at >= ?", (str(vault), since))]
        calls, last = [], 0.0
        for sid in ids:
            for tc, ts in c.execute("select tool_calls, timestamp from messages where session_id = ?", (sid,)):
                last = max(last, ts or 0)
                for x in json.loads(tc) if tc else []:
                    calls.append(str((x.get("function") or {}).get("arguments", "")))
        c.close()
        return calls, last
    except (sqlite3.Error, ValueError):
        return [], 0.0


def kill_tree(pid: int) -> None:
    """Kills a process and every descendant: hermes starts its agent in a session of its own, out of our group."""
    ps = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True).stdout.split()
    kids: dict[int, list[int]] = {}
    for a, b in zip(ps[::2], ps[1::2]):
        kids.setdefault(int(b), []).append(int(a))
    todo, seen = [pid], []
    while todo:
        p = todo.pop()
        seen.append(p)
        todo += kids.get(p, [])
    for p in reversed(seen):
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_quiet(vault: Path, since: float, limit: int = 900) -> int:
    """Waits until no session of this run has written for QUIET_S seconds; returns the seconds waited."""
    t0 = time.time()
    while time.time() - t0 < limit:
        _, last = session_calls(vault, since)
        if time.time() - last >= QUIET_S:
            break
        time.sleep(5)
    return round(time.time() - t0)


def git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True)


def ask(agent: str, reasoning: str, prompt: str, vault: Path) -> tuple[dict | None, str, dict]:
    qf = Path(tempfile.mkstemp(prefix="renewal-", suffix=".txt")[1])
    qf.write_text(prompt, encoding="utf-8")
    cmd = {"hermes": ["hermes", "chat", "-Q", "--query-file", str(qf), "--reasoning", reasoning, "-t", "terminal,file",
                      "--max-turns", str(MAX_TURNS), "--run-budget", str(TIMEOUT - 60)]}[agent]
    since = time.time() - 1
    p = subprocess.Popen(cmd, cwd=vault, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    stop, t0 = None, time.time()
    while p.poll() is None:
        calls, _ = session_calls(vault, since)
        top = Counter(calls).most_common(1)
        if top and top[0][1] >= MAX_REPEAT:
            stop = "loop"
        elif len(calls) > MAX_CALLS:
            stop = "too_many_calls"
        elif time.time() - t0 > TIMEOUT:
            stop = "timeout"
        if stop:
            kill_tree(p.pid)
            break
        time.sleep(3)
    out = p.communicate()[0] or ""
    qf.unlink(missing_ok=True)
    calls, _ = session_calls(vault, since)
    trace = {"calls": len(calls), "unique": len(set(calls)), "quiet_wait": wait_quiet(vault, since)}
    if stop:
        return None, stop, trace
    for m in reversed(list(re.finditer(r"\{[^{}]*\"verdict\"[^{}]*\}", out, re.S))):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if d.get("verdict") in VERDICTS:
            return d, out, trace
    return None, out, trace


def bench(vault: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="cortex-renewal-") as tmp:
        copy = Path(tmp) / vault.name
        shutil.copytree(vault, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".obsidian"))
        p = subprocess.run([sys.executable, "-I", "-c", lifecycle.BENCH_CHILD, str(copy), str(vault / "_brain/bench.json")],
                           capture_output=True, text=True, timeout=1800)
    return json.loads(p.stdout.strip().splitlines()[-1])


def set_fm(text: str, key: str, value: str) -> str:
    end = text.index("\n---", 3)
    fm, rest = text[:end], text[end:]
    if re.search(rf"(?m)^{key}:", fm):
        fm = re.sub(rf"(?m)^{key}:.*$", f"{key}: {value}", fm, count=1)
    else:
        fm += f"\n{key}: {value}"
    return fm + rest


def apply(vault: Path, n, d: dict, day: str) -> str:
    path = n.path if n.path.is_absolute() else vault / n.path
    text = path.read_text(encoding="utf-8")
    text = set_fm(text, "reviewed", day)
    if d["verdict"] == "changed" and (d.get("new_text") or "").strip():
        m = re.search(r"(?m)^## (?!Evidence|Connections|Details|Branches).*\n", text)
        if m:
            nxt = re.search(r"(?m)^## ", text[m.end():])
            at = m.end() + (nxt.start() if nxt else len(text) - m.end())
            text = text[:m.end()] + d["new_text"].strip() + "\n\n" + text[at:]
        text = set_fm(text, "updated", day)
        text = set_fm(text, "status", "seed")   # new content waits for the owner's validation
    ev = f"- Renewal {day}: {d['verdict']}, {str(d.get('evidence') or '')[:200]}"
    if re.search(r"(?m)^## Evidence", text):
        text = re.sub(r"(?m)^(## Evidence[ \t]*\n(?:(?!## ).*\n)*?)(?=\n*## |\Z)", lambda m: m.group(1).rstrip("\n") + "\n" + ev + "\n", text, count=1)
    path.write_text(text, encoding="utf-8")
    return d["verdict"]


def renew(a) -> int:
    skip = set(Path(a.skip_titles).read_text(encoding="utf-8").splitlines()) if a.skip_titles else set()
    only = set(Path(a.only_titles).read_text(encoding="utf-8").splitlines()) if a.only_titles else None
    vault = Path(a.vault).expanduser().resolve()
    usage_file = Path(a.usage_from).expanduser() if a.usage_from else vault / "_brain/state/usage.jsonl"
    day = today(None).isoformat()
    todo = [d for d in due(vault, today(a.as_of), usage_file) if d["node"].title not in skip
            and (only is None or d["node"].title in only)][:a.limit]
    configs = CONFIGS.get(a.config_name or vault.name, [])
    DATA.mkdir(parents=True, exist_ok=True)
    log = DATA / f"{vault.name}.jsonl"
    base = bench(vault)
    print(f"{vault.name}: {len(todo)} notes to renew · bench hit@1 {base['hit1']}/{base['n']} MRR {base['mrr']}", flush=True)
    tally = Counter()
    for i, d in enumerate(todo, 1):
        n = d["node"]
        text = re.sub(r"(?ms)^## Connections.*", "", n.body).strip()[:2500]
        intent, checks = n.fm.get("verify_intent"), n.fm.get("verify") or []
        checks = checks if isinstance(checks, list) else [checks]
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "title": n.title, "type": n.type, "age": d["age"], "ttl": d["ttl"],
               "intent": intent, "contract": len(checks)}
        early, results = None, []
        if intent:   # the note was born (or backfilled) with its contract: code decides first
            results = [(c, *run_check(c)) for c in checks]
            rec["checks"] = [f"{'ok' if r is True else 'FAIL' if r is False else 'BROKEN'} {c} :: {why}" for c, r, why in results]
            failed = [x for x in results if x[1] is False]
            if intent == "history":
                early = "history"
            elif results and all(r is True for _, r, _ in results):
                early = "still_true"
            elif failed and intent == "rule":
                early = "drift"   # the system broke the rule; the note is not rewritten
            elif not results and intent == "behavior":
                early = "needs_experiment"
            elif not results and intent == "rule":
                early = "policy"   # a standing rule with nothing to check: only Adhonys revokes it, no model rewrites it
        if early:
            rec.update(verdict=early, secs=0, stray_edits=0, by="contract")
            if early == "still_true":
                apply(vault, n, {"verdict": "still_true", "evidence": "contract: " + "; ".join(w for _, _, w in results)[:200]}, day)
                subprocess.run([sys.executable, "_brain/scripts/brain.py", "build"], cwd=vault, capture_output=True)
                git(vault, "add", "-A")
                git(vault, "commit", "-q", "-m", f"consolidation: renewal still_true by contract, {n.title}")
            tally[early] += 1
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {i}/{len(todo)} {early:16}    0s {n.title[:70]}", flush=True)
            continue
        facts, checkable = precheck(text, configs)
        if intent:
            facts = rec.get("checks", []) + facts
            checkable = checkable or len(checks)
        rec.update(checkable=checkable, facts=facts)
        if not checkable:   # behaviour or rules only: a verifier would wander; it waits for an experiment or the owner
            rec.update(verdict="needs_experiment", secs=0, stray_edits=0)
            tally[rec["verdict"]] += 1
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {i}/{len(todo)} {'needs_experiment':16}    0s {n.title[:70]}", flush=True)
            continue
        prompt = PROMPT.format(vault=vault.name, today=day, title=n.title, layer=n.layer, type=n.type, truth=n.fm.get("truth"),
                               reviewed=d["seen"], summary=n.summary, text=text, facts="\n".join(f"- {x}" for x in facts))
        t0 = dt.datetime.now()
        verdict, raw, trace = ask(a.agent, a.reasoning, prompt, vault)
        secs = round((dt.datetime.now() - t0).total_seconds())
        rec.update(trace)
        stray = [l for l in git(vault, "status", "--porcelain").stdout.splitlines() if not re.search(r"MAP\.md|_brain/(Panel|generated|state|proposals)", l)]
        if stray:   # the agent was told read-only; whatever it wrote is kept aside, not in the vault
            git(vault, "stash", "push", "-u", "-q", "-m", f"renewal: stray edits during {n.title}")
        rec.update(secs=secs, stray_edits=len(stray))
        if verdict is None:
            rec.update(verdict=raw if raw in ("loop", "too_many_calls", "timeout") else "skipped",
                       reason=raw if raw in ("loop", "too_many_calls", "timeout") else "no verdict in the reply")
        elif verdict["verdict"] in ("still_true", "changed"):
            rec.update(verdict=verdict["verdict"], evidence=verdict.get("evidence"), reason=verdict.get("reason"))
            apply(vault, n, verdict, day)
            subprocess.run([sys.executable, "_brain/scripts/brain.py", "build"], cwd=vault, capture_output=True)
            git(vault, "add", "-A")
            git(vault, "commit", "-q", "-m", f"consolidation: renewal {verdict['verdict']}, {n.title}")
            if verdict["verdict"] == "changed":
                after = bench(vault)
                rec["bench"] = f"{after['hit1']}/{after['n']} MRR {after['mrr']}"
                if after["hit1"] <= base["hit1"] - BENCH_DROP_HIT1 or after["mrr"] <= base["mrr"] - BENCH_DROP_MRR:
                    git(vault, "revert", "--no-edit", "HEAD")
                    rec["verdict"] = "changed_reverted"
                else:
                    base = after
        else:
            rec.update(verdict=verdict["verdict"], evidence=verdict.get("evidence"), reason=verdict.get("reason"))
        tally[rec["verdict"]] += 1
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  {i}/{len(todo)} {rec['verdict']:16} {secs:4}s {n.title[:70]}", flush=True)
    end = bench(vault)
    print(f"{vault.name}: {dict(tally)} · bench hit@1 {end['hit1']}/{end['n']} MRR {end['mrr']}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("due", "renew"):
        s = sub.add_parser(name)
        s.add_argument("vault")
        s.add_argument("--as-of", help="pretend today is this date (to try the cycle on a freshly reviewed vault)")
        s.add_argument("--limit", type=int, default=20)
        s.add_argument("--usage-from", help="usage.jsonl to weigh heat with (a worktree has none)")
        s.add_argument("--skip-titles", help="file of titles to leave out, one per line")
        s.add_argument("--only-titles", help="file of the only titles to renew, one per line")
        s.add_argument("--config-name", help="whose config files to check keys in (default: the vault's name)")
        if name == "renew":
            s.add_argument("--agent", default="hermes", choices=["hermes"])
            s.add_argument("--reasoning", default="medium")
    a = ap.parse_args()
    if a.cmd == "due":
        vault = Path(a.vault).expanduser().resolve()
        uf = Path(a.usage_from).expanduser() if a.usage_from else vault / "_brain/state/usage.jsonl"
        rows = due(vault, today(a.as_of), uf)
        for d in rows[:a.limit]:
            print(f"{d['age']:4}d / {d['ttl']:3}d  heat {d['heat']:3}  {d['node'].type:9} {d['node'].title[:80]}")
        print(f"{len(rows)} due")
        return 0
    return renew(a)


if __name__ == "__main__":
    sys.exit(main())
