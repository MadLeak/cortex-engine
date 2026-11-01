#!/usr/bin/env python3
"""Note renewal: the life-cycle phase that keeps current notes true. A note ages by its type and its use; when it is
due, an agent checks it against the live system (read-only) and the script, not the agent, applies the verdict.

    python3 scripts/renewal.py due VAULT [--as-of 2026-12-15] [--limit 20]
    python3 scripts/renewal.py renew VAULT --limit 10 [--as-of DATE] [--agent hermes] [--reasoning medium]

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
import tempfile
import shutil
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

PROMPT = """You are checking one note of {vault}, an AI agents' memory, against the live system on this machine
(Alastor, Ubuntu 26.04; Windows is the other boot and is not reachable now). Today is {today}.

Rules: READ-ONLY. You may run commands that only read (cat, ls, grep, --version, systemctl status, ss, curl to
localhost, git log). Never write, edit, move, install, restart or delete anything, and never print secrets: for a file
that may hold a key, check only that a field exists. Use at most 6 commands. If the note is about something you cannot
reach from here (Windows side, another host, an account), do not guess: answer unverifiable.

NOTE: {title}  (L{layer} {type}, truth {truth}, last reviewed {reviewed})
summary: {summary}
{text}

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
    out = []
    for n in v.net.values():
        t = ttl(n, heat)
        if t is None:
            continue
        seen = str(n.fm.get("reviewed") or n.fm.get("updated") or n.fm.get("created") or "")[:10]
        try:
            age = (as_of - dt.date.fromisoformat(seen)).days
        except ValueError:
            age = 10 ** 4
        if age >= t:
            out.append({"node": n, "age": age, "ttl": t, "heat": heat[n.key], "seen": seen})
    out.sort(key=lambda d: (-d["age"] / d["ttl"], -d["heat"]))   # the most overdue first, the most used among equals
    return out


def git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True)


def ask(agent: str, reasoning: str, prompt: str, vault: Path) -> tuple[dict | None, str]:
    cmd = {"hermes": ["hermes", "-z", prompt, "--in", str(vault), "--reasoning", reasoning, "-t", "terminal,file"]}[agent]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return None, "timeout"
    for m in reversed(list(re.finditer(r"\{[^{}]*\"verdict\"[^{}]*\}", out, re.S))):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if d.get("verdict") in VERDICTS:
            return d, out
    return None, out


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
    vault = Path(a.vault).expanduser().resolve()
    usage_file = Path(a.usage_from).expanduser() if a.usage_from else vault / "_brain/state/usage.jsonl"
    day = today(None).isoformat()
    todo = due(vault, today(a.as_of), usage_file)[:a.limit]
    DATA.mkdir(parents=True, exist_ok=True)
    log = DATA / f"{vault.name}.jsonl"
    base = bench(vault)
    print(f"{vault.name}: {len(todo)} notes to renew · bench hit@1 {base['hit1']}/{base['n']} MRR {base['mrr']}", flush=True)
    tally = Counter()
    for i, d in enumerate(todo, 1):
        n = d["node"]
        prompt = PROMPT.format(vault=vault.name, today=day, title=n.title, layer=n.layer, type=n.type, truth=n.fm.get("truth"),
                               reviewed=d["seen"], summary=n.summary, text=re.sub(r"(?ms)^## Connections.*", "", n.body).strip()[:2500])
        t0 = dt.datetime.now()
        verdict, raw = ask(a.agent, a.reasoning, prompt, vault)
        secs = round((dt.datetime.now() - t0).total_seconds())
        stray = [l for l in git(vault, "status", "--porcelain").stdout.splitlines() if not re.search(r"MAP\.md|_brain/(Panel|generated|state|proposals)", l)]
        if stray:   # the agent was told read-only; whatever it wrote is kept aside, not in the vault
            git(vault, "stash", "push", "-u", "-q", "-m", f"renewal: stray edits during {n.title}")
        rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "title": n.title, "type": n.type, "age": d["age"], "ttl": d["ttl"],
               "secs": secs, "stray_edits": len(stray)}
        if verdict is None:
            rec.update(verdict="skipped", reason="timeout" if raw == "timeout" else "no verdict in the reply")
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
