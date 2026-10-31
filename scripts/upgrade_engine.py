#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Installs this repository's engine into existing vaults, then runs each vault's doctor.

    python3 scripts/upgrade_engine.py ~/vaults/agent-a-cortex ~/vaults/agent-b-cortex [--dry-run]

Copies brain.py, brain_core.py, snowball.py, panel.html, lexicon_es.tsv, synonyms_en.tsv and tests/ into <vault>/_brain/scripts/.
Content, config, glossary, templates and state are left alone. Upgrade every sibling vault together: doctor fails
when siblings run different engine versions. Commit each vault afterwards with `engine: vX.Y.Z`.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "template-vault" / "_brain" / "scripts"
FILES = ("brain.py", "brain_core.py", "snowball.py", "panel.html", "lexicon_es.tsv", "synonyms_en.tsv")

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("vaults", nargs="+")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

failed = 0
for raw in a.vaults:
    dest = Path(raw).expanduser().resolve() / "_brain" / "scripts"
    if not (dest / "brain.py").is_file():
        print(f"skip   {raw}: no _brain/scripts/brain.py (create vaults with scripts/new_vault.py)")
        failed += 1
        continue
    changed = [f for f in FILES if not (dest / f).exists() or (dest / f).read_bytes() != (SRC / f).read_bytes()]
    print(f"{'would update' if a.dry_run else 'update'} {raw}: {', '.join(changed) or 'already current'}")
    if a.dry_run:
        continue
    for f in changed:
        shutil.copy2(SRC / f, dest / f)
    shutil.copytree(SRC / "tests", dest / "tests", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
for raw in ([] if a.dry_run else a.vaults):
    brain = Path(raw).expanduser().resolve() / "_brain" / "scripts" / "brain.py"
    if brain.is_file():
        r = subprocess.run([sys.executable, str(brain), "doctor"], capture_output=True, text=True)
        print(f"doctor {raw}: {(r.stdout.strip().splitlines() or ['?'])[-1]}")
        failed += r.returncode != 0
sys.exit(1 if failed else 0)
