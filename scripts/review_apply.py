#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Applies the owner's review decisions to a vault: the hub's Review page calls it, one batch per click.

    echo '{"decisions": {"<node id>": "consolidated", ...}}' | python3 scripts/review_apply.py VAULT [--out DIR]

A decision is consolidated or obsolete (the node's status changes, as `brain.py status` does), or fix or skip (only
recorded: the node stays a seed). Changed nodes are committed in one "consolidation:" commit, through the vault's
hook, under CORTEX_GIT_NAME and CORTEX_GIT_EMAIL when set. Each decision is recorded in the review store, so the page
and review_prep.py leave it alone until the node's text changes. Prints a JSON summary.
"""
import argparse
import contextlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_prep  # noqa: E402

DECISIONS = {"consolidated": "consolidated", "obsolete": "obsolete", "fix": None, "skip": None}


def apply(vault: Path, decisions: dict[str, str], out_dir: Path, owner: str) -> dict:
    bad = {k: d for k, d in decisions.items() if d not in DECISIONS}
    if bad:
        raise ValueError(f"unknown decisions: {sorted(set(bad.values()))}")
    bc, v = review_prep.load(vault)
    by_key = v.by_key()
    store = out_dir / f"{vault.name}.jsonl"
    changed, missing = [], []
    for key, d in decisions.items():
        n = by_key.get(key)
        if n is None:
            missing.append(key)
            continue
        status = DECISIONS[d]
        if status and n.status != status:
            with contextlib.redirect_stdout(sys.stderr):   # stdout carries only the JSON summary
                bc.cmd_status(v, argparse.Namespace(title=n.title, status=status))
            changed.append(n.path)
    t = datetime.now().isoformat(timespec="seconds")
    with review_prep.locked(store):
        have = review_prep.read_store(store)
        for key, d in decisions.items():
            if key in have and key not in missing:
                have[key].update(decided=d, decided_t=t)
        review_prep.write_store(store, have)
    commit = None
    if changed:
        git = ["git", "-C", str(vault)]
        for k, env in (("user.name", "CORTEX_GIT_NAME"), ("user.email", "CORTEX_GIT_EMAIL")):
            if os.environ.get(env):
                git += ["-c", f"{k}={os.environ[env]}"]
        subprocess.run(git + ["add", "--", *map(str, changed)], check=True, capture_output=True)
        n_ok = sum(1 for d in decisions.values() if d == "consolidated")
        n_ob = sum(1 for d in decisions.values() if d == "obsolete")
        node = lambda n: f"{n} node{'s' * (n != 1)}"
        parts = [f"{node(n_ok)} validated" if n_ok else "", f"{node(n_ob)} marked obsolete" if n_ob else ""]
        msg = f"consolidation: {' and '.join(p for p in parts if p)} by {owner} (review)"
        r = subprocess.run(git + ["commit", "-q", "-m", msg], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"git commit failed:\n{(r.stdout + r.stderr)[-1500:]}")
        commit = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"vault": vault.name, "decided": len(decisions) - len(missing), "changed": len(changed), "missing": missing,
            "commit": commit}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("vault")
    ap.add_argument("--out", help="review folder (default: <vault root>/.cortex-hub/review)")
    ap.add_argument("--owner", default=os.environ.get("CORTEX_OWNER", "the owner"))
    a = ap.parse_args()
    vault = Path(a.vault).expanduser().resolve()
    out_dir = Path(a.out) if a.out else vault.parent / ".cortex-hub" / "review"
    try:
        out = apply(vault, (json.load(sys.stdin) or {}).get("decisions") or {}, out_dir, a.owner)
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
