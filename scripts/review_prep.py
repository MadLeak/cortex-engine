#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Prepares the owner's review of a vault's seeds: usage signals, then a verdict per node from the local Strata model.

    python3 scripts/review_prep.py ~/Vaults/"Agents Cortex"            # every seed not yet judged, or changed since
    python3 scripts/review_prep.py VAULT --limit 5 --show-prompt        # try the request on a few nodes

Nothing in the vault changes: verdicts go to <vault root>/.cortex-hub/review/<vault>.jsonl, which the hub's Review page
shows, and only the owner's click there marks a node consolidated or obsolete. A node is judged again when its text
changes. Strata gets one node per request with everything it needs to judge it alone: the vault's purpose, the node,
the chain of ancestors it hangs from, the notes it links to, the titles of its siblings (to spot duplicates), its
truth and how agents used it. Ancestors rather than siblings' summaries: a 2026-10-31 test on 20 seeds gave the same
or better verdicts in two thirds of the time. A note with no valid truth is settled by rule, without a model. It
answers checks first, then a verdict, so the verdict rests on them. Progress goes to review/progress.json (the wallpaper's Strata office shows it).
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

STRATA = os.environ.get("CORTEX_STRATA", "http://localhost:8080")
STRATA_KEY = Path(os.environ.get("CORTEX_STRATA_KEY", Path.home() / ".config/strata/api.key"))
ACTIONS = ("consolidate", "fix", "obsolete", "merge")
META = ("title", "layer", "summary", "domain", "truth", "reads", "used")   # shown on the Review page
CHECKS = ("one_idea", "specific", "fits_parent", "english", "dated_if_current", "still_useful")
TRUTHS = ("current", "historical", "superseded")

PROMPT = """You review one note of {vault}, the long-term memory that AI agents read for {owner}. The owner will mark
the note CONSOLIDATED (verified and kept as is) only on your recommendation, so be strict: when in doubt, say fix.

The vault is a graph of short English Markdown notes in layers: L1 = a domain (a synthesis of its branches), L2 = a
concept (one standing fact or rule), L3 = a detail (one lesson, fact, decision, procedure or example). Each note has a
parent ("up"). truth: current = describes the present and must name a date or evidence; historical = a dated record
of the past, valid as long as it says when; superseded = wrong now.

Judge the NOTE against these checks (true or false):
- one_idea: it holds one idea; an L1 may summarise many.
- specific: concrete and checkable (names, numbers, paths, commands, dates), not vague advice.
- fits_parent: it belongs under its parent and does not contradict an ancestor, a linked note or a sibling.
- english: written in English (names and quoted commands may be in any language).
- dated_if_current: a present-tense claim carries a date or evidence; true when truth is historical.
- still_useful: an agent would still want it, or it is a dated record worth keeping.

Then one action:
- consolidate: every check is true.
- fix: worth keeping but a check fails; say what to change.
- obsolete: no lasting value (a placeholder, a stale one-off status, fully repeated by its parent).
- merge: it says the same as one SIBLING; give that sibling's exact title as "target".

Answer with one JSON object and nothing else:
{{"checks": {{"one_idea": true, "specific": true, "fits_parent": true, "english": true, "dated_if_current": true, "still_useful": true}},
 "action": "consolidate|fix|obsolete|merge", "target": null, "reason": "<one English sentence, at most 160 characters>"}}

NOTE
title: {title}
layer: L{layer} · type: {type} · truth: {truth} · created: {created}
summary: {summary}
used by agents: {reads} reads, {used} times credited as helpful{contradicts}
text:
{text}

ANCESTORS (the chain this note hangs from, top first)
{ancestors}

LINKED NOTES (the notes this note itself points at, with the link type)
{linked}

SIBLINGS (other notes under the same parent, titles only)
{siblings}
"""


@contextlib.contextmanager
def locked(store: Path):
    """Holds the store's lock: this script and the hub's apply both rewrite it."""
    try:
        import fcntl
    except ImportError:   # Windows: no lock, the hub runs in its container anyway
        yield
        return
    with open(store.with_suffix(".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_store(store: Path) -> dict[str, dict]:
    have = {}
    if store.exists():
        for line in store.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                have[d["id"]] = d
            except (ValueError, KeyError):
                continue
    return have


def write_store(store: Path, have: dict[str, dict]) -> None:
    tmp = store.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in have.values()), encoding="utf-8")
    os.replace(tmp, store)


def load(vault: Path):
    sys.path.insert(0, str(vault / "_brain/scripts"))
    import brain_core as bc
    return bc, bc.Vault(vault)


def usage(vault: Path) -> tuple[Counter, Counter]:
    reads, used = Counter(), Counter()
    p = vault / "_brain/state/usage.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            for k in e.get("keys") or ([e["k"]] if e.get("k") else []):
                (reads if e.get("e") == "read" else used if e.get("e") == "used" else Counter())[k] += 1
    return reads, used


def clean(body: str, limit: int) -> str:
    body = re.sub(r"%%.*?%%", "", body, flags=re.S)
    body = re.sub(r"(?ms)^## Connections.*", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()[:limit]


def items(vault: Path, owner: str = "its owner") -> list[dict]:
    """Every seed with the request that judges it, and a hash of what the request depends on."""
    bc, v = load(vault)
    reads, used = usage(vault)
    cfg = json.loads((vault / "_brain/config.json").read_text(encoding="utf-8"))
    nodes = [n for n in v.net.values() if n.layer in (1, 2, 3)]
    by_title = {n.title: n for n in nodes}
    kids: dict[str, list] = {}
    for n in nodes:
        kids.setdefault(n.up or "", []).append(n)
    out = []
    for n in nodes:
        if n.status != "seed":
            continue
        chain, p = [], by_title.get(n.up or "")
        while p and p not in chain:   # a cycle of ups would loop forever
            chain.append(p)
            p = by_title.get(p.up or "")
        linked = [(s.type, by_title[s.target]) for s in n.synapses if s.target in by_title]
        sibs = [s for s in kids.get(n.up or "", []) if s is not n][:20]
        contra = [s.target for s in n.synapses if s.type == "contradicts"]
        prompt = PROMPT.format(
            vault=cfg.get("vault") or vault.name, owner=owner,
            title=n.title, layer=n.layer, type=n.type or "-", truth=n.fm.get("truth") or "-", created=n.fm.get("created") or "-",
            summary=n.summary, reads=reads[n.key], used=used[n.key],
            contradicts=f"\ncontradicts: {', '.join(contra)}" if contra else "",
            text=clean(n.body, 2200) or "(empty)",
            ancestors="\n".join(f"- L{a.layer} {a.title}: {a.summary}" for a in reversed(chain)) or "(none: a top domain)",
            linked="\n".join(f"- {t} → {m.title} (L{m.layer}, truth {m.fm.get('truth') or '-'}): {m.summary}"
                             for t, m in linked) or "(none)",
            siblings="\n".join(f"- {s.title}" for s in sibs) or "(none)")
        rel = n.path.relative_to(vault) if n.path.is_absolute() else n.path
        domain = rel.parts[0] if len(rel.parts) > 1 else ""
        out.append({"id": n.key, "title": n.title, "layer": n.layer, "summary": n.summary, "domain": domain,
                    "truth": n.fm.get("truth"), "reads": reads[n.key], "used": used[n.key], "contradicts": contra,
                    "prompt": prompt, "hash": hashlib.sha1(re.sub(r"(?m)^used by agents:.*$", "", prompt).encode()).hexdigest()[:12]})
    return out


def rule(it: dict) -> dict | None:
    """What the signals settle without a model."""
    if it["contradicts"]:
        return {"action": "fix", "reason": f"contradicts {it['contradicts'][0]}: decide which one holds"}
    if it["truth"] == "superseded":
        return {"action": "obsolete", "reason": "marked superseded: wrong now"}
    if it["truth"] not in TRUTHS:   # models judged these unevenly; the fix is always the same
        return {"action": "fix", "reason": f"truth is '{it['truth'] or '-'}': set current (with a date or evidence) or historical"}
    return None


def strata(prompt: str) -> dict:
    body = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 400,
            "chat_template_kwargs": {"enable_thinking": False}}
    headers = {"Content-Type": "application/json"}
    if STRATA_KEY.exists():   # Strata asks for a Bearer key on the API; it is passed unread
        headers["Authorization"] = f"Bearer {STRATA_KEY.read_text().strip()}"
    req = urllib.request.Request(f"{STRATA}/v1/chat/completions", json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        reply = json.load(r)["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", reply, re.S)
    d = json.loads(m.group(0)) if m else {}
    action = str(d.get("action", "")).strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"no verdict in: {reply[:200]}")
    checks = {k: bool((d.get("checks") or {}).get(k)) for k in CHECKS}
    if action == "consolidate" and not all(checks.values()):   # its own checks overrule a lenient verdict
        action = "fix"
    return {"action": action, "target": d.get("target") or None, "checks": checks, "reason": str(d.get("reason") or "")[:200]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("vault")
    ap.add_argument("--limit", type=int, default=0, help="judge at most this many nodes")
    ap.add_argument("--show-prompt", action="store_true", help="print the first request")
    ap.add_argument("--out", help="review folder (default: <vault root>/.cortex-hub/review)")
    ap.add_argument("--owner", default=os.environ.get("CORTEX_OWNER", "its owner"), help="whose memory it is, for the request")
    a = ap.parse_args()
    vault = Path(a.vault).expanduser().resolve()
    out_dir = Path(a.out) if a.out else vault.parent / ".cortex-hub" / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    store = out_dir / f"{vault.name}.jsonl"
    with locked(store):
        have = read_store(store)
    todo = items(vault, a.owner)
    live = {it["id"] for it in todo}
    have = {k: d for k, d in have.items() if k in live or d.get("decided")}   # keep the owner's decisions
    for it in todo:   # fresh titles, places and usage for the verdicts already given
        if it["id"] in have:
            have[it["id"]].update({k: it[k] for k in META})
    pending = [it for it in todo if have.get(it["id"], {}).get("hash") != it["hash"]]   # new, or its text changed
    pending.sort(key=lambda it: (-(it["used"] * 3 + it["reads"]), it["layer"]))   # the most used first
    if a.limit:
        pending = pending[:a.limit]
    if a.show_prompt and pending:
        print(pending[0]["prompt"])
    progress = out_dir / "progress.json"

    def save():
        with locked(store):   # the owner may have decided some meanwhile, on the hub's Review page
            for k, d in read_store(store).items():
                if d.get("decided") and (k not in have or have[k].get("hash") == d.get("hash")):
                    have[k] = d
            write_store(store, have)

    print(f"{vault.name}: {len(todo)} seeds, {len(pending)} to judge", flush=True)
    failed = 0
    for i, it in enumerate(pending, 1):
        progress.write_text(json.dumps({"task": f"Revisando {vault.name}", "done": i - 1, "total": len(pending),
                                        "t": time.time()}), encoding="utf-8")
        verdict, by = rule(it), "rule"
        if verdict is None:
            try:
                verdict, by = strata(it["prompt"]), "strata"
            except (OSError, ValueError) as ex:
                failed += 1
                print(f"  {it['title']}: {ex}", flush=True)
                continue
        keep = {k: it[k] for k in ("id", "hash", *META)}
        have[it["id"]] = {**keep, **verdict, "by": by, "t": datetime.now().isoformat(timespec="seconds")}
        print(f"  {i}/{len(pending)} {verdict['action']:11} {it['title'][:70]}", flush=True)
        if i % 5 == 0:
            save()
    save()
    progress.write_text(json.dumps({"task": "", "done": len(pending), "total": len(pending), "t": time.time()}), encoding="utf-8")
    print(f"{vault.name}: judged {len(pending) - failed}, failed {failed}, verdicts in {store}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
