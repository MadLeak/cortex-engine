#!/usr/bin/env python3
"""Cortex life cycle: the mechanical sleep phases of every Cortex vault on this machine, run by systemd user timers.

  nap    (hourly, phase 1)  mirror each vault's usage log into a durable history, rebuild MAP/panels when anything
                            changed, lint.
  night  (nightly, phase 2) nap, then `brain.py consolidate` (proposals.md), bench on a throwaway copy with a
                            regression alarm, a layer report (fan-out and traffic), and the one mechanical change
                            allowed without Adhonys: pruning dead synapses, only once the usage history spans
                            PRUNE_MIN_HISTORY_DAYS and at most PRUNE_MAX_PER_NIGHT per vault, committed on its own.
  rem    (nightly, phase 4) dreams the questions each note answers (doc2query) with the local Strata model, hottest
                            notes first, within a time budget; kept in ~/.local/share/cortex-life/rem for
                            `brain.py questions import` (engine 2.7.0+). Skipped when Strata is off.
  status                    last runs, history span, bench trend and REM progress per vault.
  install                   copies this script to ~/.local/lib/cortex-life and enables the three timers.

Only REM calls a model (the local one); nothing here marks a node consolidated, or writes outside the vaults' own engine commands; data lives in
~/.local/share/cortex-life (usage mirror, bench history, nightly reports, log)."""
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
VAULTS = Path(os.environ.get("CORTEX_VAULTS", HOME / "Vaults"))
DATA = Path(os.environ.get("CORTEX_LIFE_DATA", HOME / ".local/share/cortex-life"))
LIB = HOME / ".local/lib/cortex-life"
UNITS = HOME / ".config/systemd/user"
PRUNE_MIN_HISTORY_DAYS = 90    # the engine's prune rule means "no joint use in 90 days": only true with 90 days of log
PRUNE_MAX_PER_NIGHT = 10
BENCH_DROP_HIT1 = 3            # alarm when hit@1 falls this many questions below the best of the last 14 nights
BENCH_DROP_MRR = 0.03


def now():
    return dt.datetime.now().replace(microsecond=0)


def log(msg):
    DATA.mkdir(parents=True, exist_ok=True)
    line = f"{now().isoformat()} {msg}"
    print(line)
    with open(DATA / "life.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def vaults():
    return sorted(p for p in VAULTS.iterdir() if (p / "_brain/scripts/brain.py").is_file()) if VAULTS.is_dir() else []


def busy(v):
    """A git operation in flight: leave the vault alone this round."""
    g = v / ".git"
    return any((g / x).exists() for x in ("index.lock", "MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD"))


def brain(v, *args, timeout=600):
    r = subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), *args], cwd=v,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def phase(v, event, *args, nodes=None):
    """Tell the vault's panel a phase is running (`brain.py activity`, engine 2.10.0+; older engines just refuse it)."""
    try:
        if nodes is None:
            return brain(v, "activity", event, *args, timeout=60)
        r = subprocess.run([sys.executable, str(v / "_brain/scripts/brain.py"), "activity", event, "--nodes", "-", *args], cwd=v,
                           input="\n".join(nodes), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


# ---------------------------------------------------------------- phase 1: nap
def mirror_usage(v, state):
    """Append the lines usage.jsonl gained since the last nap to DATA/usage/<vault>/usage.jsonl (the vault's own log is
    gitignored, so a fresh clone starts it empty). A log that shrank was replaced: it is mirrored from its start."""
    src = v / "_brain/state/usage.jsonl"
    st = state.setdefault("usage", {}).setdefault(v.name, {"off": 0, "ino": None})
    if not src.exists():
        return 0
    info = src.stat()
    if info.st_ino != st["ino"] or info.st_size < st["off"]:
        st.update(off=0, ino=info.st_ino)
    if info.st_size == st["off"]:
        return 0
    with open(src, "rb") as f:
        f.seek(st["off"])
        chunk = f.read()
    cut = chunk.rfind(b"\n") + 1
    if not cut:
        return 0
    dest = DATA / "usage" / v.name / "usage.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "ab") as f:
        f.write(chunk[:cut])
    st["off"] += cut
    return chunk[:cut].count(b"\n")


def stamp(v):
    """Newest change among the vault's notes and its usage log: a nap rebuilds only when it moved."""
    newest = 0.0
    for p in v.rglob("*.md"):
        if ".git" in p.parts or "_brain" in p.parts or p.name == "MAP.md":
            continue
        newest = max(newest, p.stat().st_mtime)
    u = v / "_brain/state/usage.jsonl"
    return max(newest, u.stat().st_mtime if u.exists() else 0.0)


def nap(state, report=None):
    for v in vaults():
        if busy(v):
            log(f"nap {v.name}: git busy, skipped")
            continue
        added = mirror_usage(v, state)
        phase(v, "rested")
        s = stamp(v)
        st = state.setdefault("built", {})
        if s > st.get(v.name, 0):
            code, out = brain(v, "build")
            lint_code, lint_out = brain(v, "lint")
            st[v.name] = s
            log(f"nap {v.name}: +{added} usage lines · build {'ok' if code == 0 else 'FAILED'} · {lint_out.splitlines()[-1] if lint_out else ''}")
            if report is not None and (code or lint_code):
                report["alerts"].append(f"{v.name}: build/lint failed — {(out or lint_out).splitlines()[-1]}")
        elif added:
            log(f"nap {v.name}: +{added} usage lines")
    state["last_nap"] = now().isoformat()


# ---------------------------------------------------------------- phase 2: night
def history_span_days(v):
    p = DATA / "usage" / v.name / "usage.jsonl"
    if not p.exists():
        return 0
    with open(p, encoding="utf-8") as f:
        first = f.readline()
    try:
        t0 = dt.datetime.fromisoformat(json.loads(first)["t"])
    except (ValueError, KeyError):
        return 0
    return (now() - t0).days


def consolidate(v, report):
    code, out = brain(v, "consolidate")
    counts = dict(re.findall(r"(\w+) (\d+)", out.split("·", 1)[1])) if code == 0 and "·" in out else {}
    report["vaults"][v.name]["proposals"] = {k: int(x) for k, x in counts.items()}
    if code:
        report["alerts"].append(f"{v.name}: consolidate failed — {out.splitlines()[-1] if out else code}")
    return code == 0


def auto_prune(v, report):
    span = history_span_days(v)
    r = report["vaults"][v.name]
    r["history_days"] = span
    p = v / "_brain/proposals.md"
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8").splitlines()
    candidates = [i for i, l in enumerate(lines) if l.startswith("- [ ] prune :: ")]
    if not candidates:
        return
    if span < PRUNE_MIN_HISTORY_DAYS:
        r["prune"] = f"{len(candidates)} proposed, held: usage history spans {span} of {PRUNE_MIN_HISTORY_DAYS} days"
        return
    if git(v, "diff", "--cached", "--name-only"):
        r["prune"] = f"{len(candidates)} proposed, held: the index has staged work that is not the cycle's"
        return
    before = set(git(v, "diff", "--name-only").split("\n"))
    for i in candidates[:PRUNE_MAX_PER_NIGHT]:
        lines[i] = lines[i].replace("- [ ]", "- [x]", 1)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, out = brain(v, "consolidate", "--apply")
    changed = [f for f in git(v, "diff", "--name-only").split("\n") if f and f not in before and f.endswith(".md")
               and not f.startswith(("_brain/", "MAP.md"))]
    if code or not changed:
        report["alerts"].append(f"{v.name}: nightly prune did not apply — {out.splitlines()[-1] if out else code}")
        return
    msg = f"consolidation: nightly prune {now():%Y-%m-%d} ({len(changed)} notes)"
    # stage only the pruned notes; the vault's pre-commit hook adds the outputs it regenerates (MAP, panels, index)
    subprocess.run(["git", "add", "--", *changed], cwd=v, capture_output=True)
    c = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=v, capture_output=True, text=True)
    r["prune"] = f"{min(len(candidates), PRUNE_MAX_PER_NIGHT)} pruned, committed" if c.returncode == 0 else "prune applied, commit FAILED"
    if c.returncode:
        report["alerts"].append(f"{v.name}: prune commit failed — {c.stderr.strip()[-200:]}")


def git(v, *args):
    return subprocess.run(["git", *args], cwd=v, capture_output=True, text=True).stdout.strip()


BENCH_CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1] + "/_brain/scripts")
import brain_core as bc
v = bc.Vault(sys.argv[1]); w = bc.Telemetry(v).weights()
qs = json.loads(open(sys.argv[2], encoding="utf-8").read())["questions"]
rows = []
for q in qs:
    t = {bc.norm(x) for x in q["targets"]}
    for lang in ("en", "es"):
        if not q.get(lang): continue
        ranked = [bc.norm(n.title) for _, n in bc.score(v, q[lang], w)]
        rows.append(next((r for r, x in enumerate(ranked, 1) if x in t), None))
n = len(rows) or 1
print(json.dumps({"engine": bc.VERSION, "n": len(rows), "hit1": sum(r == 1 for r in rows),
                  "hit5": sum(bool(r) and r <= 5 for r in rows), "mrr": round(sum(1 / r for r in rows if r) / n, 4)}))
'''


def bench(v, report):
    """The vault's bench on a throwaway copy, so no recall reaches the live telemetry; appended to its history."""
    qf = v / "_brain/bench.json"
    r = report["vaults"][v.name]
    if not qf.exists():
        r["bench"] = "no bench.json"
        return
    with tempfile.TemporaryDirectory(prefix="cortex-bench-") as tmp:
        copy = Path(tmp) / v.name
        shutil.copytree(v, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".obsidian"))
        p = subprocess.run([sys.executable, "-I", "-c", BENCH_CHILD, str(copy), str(qf)], capture_output=True, text=True, timeout=1800)
    if p.returncode:
        report["alerts"].append(f"{v.name}: bench failed — {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else p.returncode}")
        return
    res = json.loads(p.stdout.strip().splitlines()[-1])
    res.update(t=now().isoformat(), head=git(v, "rev-parse", "--short", "HEAD"))
    hist_p = DATA / "bench" / f"{v.name}.jsonl"
    hist = [json.loads(l) for l in hist_p.read_text().splitlines() if l.strip()] if hist_p.exists() else []
    recent = [h for h in hist[-14:] if h.get("n") == res["n"]]
    if recent:
        best1, prev = max(h["hit1"] for h in recent), recent[-1]
        if res["hit1"] <= best1 - BENCH_DROP_HIT1 or res["mrr"] <= prev["mrr"] - BENCH_DROP_MRR:
            report["alerts"].append(f"{v.name}: bench regression — hit@1 {res['hit1']}/{res['n']} (best of 14 nights {best1}), "
                                    f"MRR {res['mrr']} (last {prev['mrr']}) at {res['head']}")
    hist_p.parent.mkdir(parents=True, exist_ok=True)
    with open(hist_p, "a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    fell = bool(recent) and (res["hit1"] <= best1 - BENCH_DROP_HIT1 or res["mrr"] <= prev["mrr"] - BENCH_DROP_MRR)
    phase(v, "measured", "--note", f"hit@1 {res['hit1']}/{res['n']} · MRR {res['mrr']}", *(["--alarm"] if fell else []))
    trend = f" (last {recent[-1]['hit1']})" if recent else " (first run)"
    r["bench"] = f"hit@1 {res['hit1']}/{res['n']}{trend} · hit@5 {res['hit5']} · MRR {res['mrr']} · engine {res['engine']}"


def layers(v, report):
    """Fan-out of each L1/L2 and the traffic (reads and credits, whole mirrored history) through each top domain."""
    g = load_json(v / "_brain/generated/graph.json", {"nodes": []})
    N = {n["title"]: n for n in g["nodes"]}
    by_id = {n["id"]: n for n in g["nodes"]}
    kids = Counter(n.get("up") for n in g["nodes"])

    def chain(n):
        out = []
        while n is not None:
            out.append(n)
            n = N.get(n.get("up"))
        return out
    wide = sorted(((kids[n["title"]], n["layer"], n["title"]) for n in g["nodes"] if n["layer"] == 2 and kids[n["title"]] > 12), reverse=True)
    empty = sum(1 for n in g["nodes"] if n["layer"] == 2 and kids[n["title"]] == 0)
    traffic, total = Counter(), 0
    up = DATA / "usage" / v.name / "usage.jsonl"
    if up.exists():
        for line in up.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            n = by_id.get(e.get("k"))
            if n and e.get("e") in ("read", "used"):
                total += 1
                traffic[chain(n)[-1]["title"]] += 1
    size = Counter(chain(n)[-1]["title"] for n in g["nodes"])
    r = report["vaults"][v.name]
    r["layers"] = {"nodes": len(g["nodes"]), "wide_l2": [f"{t} ({k})" for k, _, t in wide[:8]], "empty_l2": empty,
                   "domains": [(t, round(size[t] / max(1, len(g["nodes"])), 2), round(traffic[t] / total, 2) if total else 0)
                               for t, _ in size.most_common()], "traffic_events": total}


def write_report(report):
    d = DATA / "reports"
    d.mkdir(parents=True, exist_ok=True)
    out = [f"# Cortex night {report['date']}", "", f"REM questions stored: {rem_summary()}", ""]
    out += ["## Alerts", *(f"- {a}" for a in report["alerts"])] if report["alerts"] else ["No alerts."]
    for name, r in report["vaults"].items():
        out += ["", f"## {name}"]
        if "bench" in r:
            out.append(f"- bench: {r['bench']}")
        if r.get("proposals"):
            out.append("- proposals: " + " · ".join(f"{k} {x}" for k, x in r["proposals"].items()))
        if "prune" in r:
            out.append(f"- prune: {r['prune']}")
        L = r.get("layers")
        if L:
            out.append(f"- layers: {L['nodes']} nodes · empty L2 {L['empty_l2']} · L2 over 12 details: {', '.join(L['wide_l2']) or 'none'}")
            out.append(f"- domains (share of nodes / of {L['traffic_events']} reads+credits so far): "
                       + " · ".join(f"{t} {s:.0%}/{x:.0%}" for t, s, x in L["domains"]))
    p = d / f"{report['date']}.md"
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p


def notify(title, body):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Cortex", title, body], capture_output=True)


def night(state):
    report = {"date": f"{now():%Y-%m-%d}", "alerts": [], "vaults": defaultdict(dict)}
    nap(state, report)
    for v in vaults():
        if busy(v):
            report["alerts"].append(f"{v.name}: git busy at night, skipped")
            continue
        report["vaults"][v.name]
        if consolidate(v, report):
            auto_prune(v, report)
        bench(v, report)
        layers(v, report)
    p = write_report(report)
    state["last_night"] = now().isoformat()
    log(f"night: {len(report['alerts'])} alerts · report {p}")
    if report["alerts"]:
        notify("Cortex: night with alerts", "\n".join(report["alerts"][:5]) + f"\n{p}")


# ---------------------------------------------------------------- phase 4: REM
STRATA = os.environ.get("CORTEX_STRATA", "http://localhost:8080")
STRATA_KEY = Path(os.environ.get("CORTEX_STRATA_KEY", Path.home() / ".config/strata/api.key"))
REM_MINUTES = 120
REM_BATCH = 3
REM_PROMPT = """For each note below, write 5 English and 5 Spanish questions that a person or an agent would type to find it
and that it specifically answers. Vary the wording (synonyms, everyday words; at most one question reuses the title),
include one 3-5 word keyword query, one question about a concrete fact (name, number, command, date) and one how/why
question. Spanish ones are natural Latin American Spanish written on their own, not translations; names, commands and
identifiers stay as they are. Output only JSON Lines, one per note, nothing else:
{"id": "<ID as given>", "en": [5 questions], "es": [5 questions]}"""
REM_CHILD = r'''
import hashlib, json, re, sys
sys.path.insert(0, sys.argv[1] + "/_brain/scripts")
import brain_core as bc
v = bc.Vault(sys.argv[1]); w = bc.Telemetry(v).weights()
own = getattr(bc, "own_text", None)
out = []
for n in v.net.values():
    if n.layer not in getattr(bc, "LAYER_ROLE", (1, 2, 3)) or n.status == "obsolete": continue
    text = re.sub(r"\s+", " ", own(n.body) if own else re.sub(r"(?ms)^## Connections.*", "", n.body)).strip()[:1100]
    # the staleness hash must not depend on the engine version: a port to an engine with own_text would redo every note
    plain = re.sub(r"\s+", " ", re.sub(r"(?ms)^## Connections.*", "", n.body)).strip()
    h = hashlib.sha1(f"{n.title}\n{n.summary}\n{plain}".encode()).hexdigest()[:12]
    out.append({"id": n.key, "title": n.title, "summary": n.summary, "text": text, "hash": h, "w": w.get(n.key, 0.0)})
print(json.dumps(out, ensure_ascii=False))
'''


def strata_up(url=None):
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url or STRATA}/health", timeout=5) as r:
            return json.load(r).get("status") == "ok"
    except (OSError, ValueError):
        return False


def strata_questions(notes, url=None, key_file=None):
    """url and key_file default to this module's (the host service); the hub passes its own."""
    import urllib.request
    text = REM_PROMPT + "\n\n" + "\n".join(f"### ID: {n['id']}\nTITLE: {n['title']}\nSUMMARY: {n['summary']}\nTEXT: {n['text']}\n" for n in notes)
    body = {"messages": [{"role": "user", "content": text}], "temperature": 0.7, "max_tokens": 700 * len(notes),
            "chat_template_kwargs": {"enable_thinking": False}}
    headers = {"Content-Type": "application/json"}
    key_file = Path(key_file) if key_file else STRATA_KEY
    if key_file.exists():   # Strata asks for a Bearer key on the API (not on /health); it is passed unread
        headers["Authorization"] = f"Bearer {key_file.read_text().strip()}"
    req = urllib.request.Request(f"{url or STRATA}/v1/chat/completions", json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=900) as r:
        reply = json.load(r)["choices"][0]["message"]["content"]
    wanted, got = {n["id"] for n in notes}, {}
    for m in re.finditer(r"\{[^{}]*\}", reply):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if str(d.get("id")) in wanted:
            langs = {}
            for lang in ("en", "es"):
                qs = [str(q).strip() for q in d.get(lang) or [] if len(str(q).strip()) > 3]
                langs[lang] = list(dict.fromkeys(qs))[:5]
            if langs["en"] or langs["es"]:
                got[str(d["id"])] = langs
    return got


def rem(state, minutes=REM_MINUTES):
    """Dream the questions each note answers (doc2query) with the local model, hottest notes first, within a time
    budget. They are kept in DATA/rem/<vault>/questions.jsonl, ready for `brain.py questions import` (engine 2.7.0+);
    nothing is written into a vault. Strata is never started here: when it is off, REM waits for another night."""
    if not strata_up():
        state["rem"] = {"t": now().isoformat(), "result": "Strata off, skipped"}
        return log("rem: Strata is not answering, skipped")
    todo, stores, where = [], {}, {}
    for v in vaults():
        where[v.name] = v
        p = subprocess.run([sys.executable, "-I", "-c", REM_CHILD, str(v)], capture_output=True, text=True, timeout=600)
        if p.returncode:
            log(f"rem {v.name}: node listing failed")
            continue
        nodes = json.loads(p.stdout)
        sp = DATA / "rem" / v.name / "questions.jsonl"
        have = {}
        if sp.exists():
            for line in sp.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    have[d["id"]] = d
                except (ValueError, KeyError):
                    continue
        live = {n["id"] for n in nodes}
        stores[v.name] = (sp, {k: d for k, d in have.items() if k in live})
        todo += [(v.name, n) for n in nodes if have.get(n["id"], {}).get("hash") != n["hash"]]
    size = Counter(name for name, _ in todo)   # small vaults finish first; within one, the hottest notes go first
    todo.sort(key=lambda x: (size[x[0]], x[0], -x[1]["w"]))
    deadline, done, failed = time.time() + minutes * 60, Counter(), 0
    i = 0
    while i < len(todo) and time.time() < deadline:
        name = todo[i][0]
        batch = [n for vn, n in todo[i:i + REM_BATCH] if vn == name]
        i += len(batch)
        try:
            got = strata_questions(batch)
        except OSError as ex:
            failed += len(batch)
            log(f"rem {name}: Strata call failed ({ex}); stopping")
            break
        sp, have = stores[name]
        dreamed = [n["id"] for n in batch if n["id"] in got]
        if dreamed:
            phase(where[name], "dreamed", "--note", f"REM {done[name] + len(dreamed)} notes tonight", nodes=dreamed)
        for n in batch:
            if n["id"] in got:
                have[n["id"]] = {"id": n["id"], "hash": n["hash"], **got[n["id"]], "t": now().isoformat()}
                done[name] += 1
            else:
                failed += 1
        sp.parent.mkdir(parents=True, exist_ok=True)
        tmp = sp.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in have.values()), encoding="utf-8")
        os.replace(tmp, sp)
    left = len(todo) - sum(done.values())
    state["rem"] = {"t": now().isoformat(), "done": dict(done), "failed": failed, "left": left}
    log(f"rem: {sum(done.values())} notes dreamed {dict(done)} · {failed} unparsed · {left} left for later nights")


def rem_summary():
    out = []
    for v in vaults():
        sp = DATA / "rem" / v.name / "questions.jsonl"
        out.append(f"{v.name} {sum(1 for _ in open(sp, encoding='utf-8')) if sp.exists() else 0}")
    return " · ".join(out)


# ---------------------------------------------------------------- status / install
def status(state):
    print(f"last nap {state.get('last_nap', '—')} · last night {state.get('last_night', '—')} · data {DATA}")
    for v in vaults():
        hist_p = DATA / "bench" / f"{v.name}.jsonl"
        hist = [json.loads(l) for l in hist_p.read_text().splitlines() if l.strip()] if hist_p.exists() else []
        trend = " → ".join(str(h["hit1"]) for h in hist[-5:]) or "—"
        print(f"  {v.name:16s} usage history {history_span_days(v):3d} d · bench hit@1 {trend}")
    print(f"REM {load_json(DATA / 'rem-state.json', {}).get('rem', '—')} · stored: {rem_summary()}")
    reps = sorted((DATA / "reports").glob("*.md"))
    if reps:
        print(f"latest report: {reps[-1]}")


UNIT = """[Unit]
Description=Cortex {what}

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart=/usr/bin/python3 {lib}/lifecycle.py {cmd}
"""
TIMER = """[Unit]
Description=Cortex {what}

[Timer]
OnCalendar={cal}
Persistent=true
RandomizedDelaySec={jitter}

[Install]
WantedBy=timers.target
"""


def install():
    LIB.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), LIB / "lifecycle.py")
    UNITS.mkdir(parents=True, exist_ok=True)
    for name, what, cmd, cal, jitter in (("cortex-nap", "nap: usage mirror, rebuild and lint (phase 1)", "nap", "hourly", "5min"),
                                         ("cortex-night", "night: proposals, bench, layers, safe prune (phase 2)", "night", "*-*-* 03:00", "10min"),
                                         ("cortex-rem", "REM: doc2query questions with the local model (phase 4)", "rem", "*-*-* 01:00", "10min")):
        (UNITS / f"{name}.service").write_text(UNIT.format(what=what, lib=LIB, cmd=cmd))
        (UNITS / f"{name}.timer").write_text(TIMER.format(what=what, cal=cal, jitter=jitter))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "cortex-nap.timer", "cortex-night.timer", "cortex-rem.timer"], check=True)
    print(f"installed {LIB / 'lifecycle.py'} · timers cortex-nap (hourly), cortex-rem (01:00) and cortex-night (03:00) enabled")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["nap", "night", "rem", "status", "install"])
    ap.add_argument("--minutes", type=float, default=REM_MINUTES, help="REM time budget")
    a = ap.parse_args()
    if a.cmd == "install":
        return install()
    import fcntl   # POSIX only; the hub imports this module for REM's model calls and never needs it
    DATA.mkdir(parents=True, exist_ok=True)
    if a.cmd == "rem":   # reads the vaults only, so it has its own lock and never holds up a nap
        with open(DATA / ".rem.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return log("rem: already running, skipped")
            state = load_json(DATA / "rem-state.json", {})
            try:
                rem(state, a.minutes)
            finally:
                save_json(DATA / "rem-state.json", state)
        return
    with open(DATA / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if a.cmd == "nap" else 0))
        except BlockingIOError:
            return log("nap: another cycle is running, skipped")
        state = load_json(DATA / "state.json", {})
        if a.cmd == "status":
            return status(state)
        try:
            (nap if a.cmd == "nap" else night)(state)
        finally:
            save_json(DATA / "state.json", state)


if __name__ == "__main__":
    sys.exit(main())
