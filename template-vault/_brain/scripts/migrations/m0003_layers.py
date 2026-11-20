# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Migration 0003: a vault states how many layers it has.

`layers: 3` goes into _brain/config.json (engine 2.25): every vault from before has exactly three levels (domain,
concept, detail), so this changes no note and no folder. Frozen: what schema 3 means. Contract: see m0001_baseline.py.
"""
import json
from pathlib import Path

ID = 3
TITLE = "the vault states its number of layers"


def _config(root: Path) -> dict:
    path = root / "_brain" / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def plan(root: Path) -> list[str]:
    return [] if "layers" in _config(root) else ["_brain/config.json"]


def up(root: Path) -> None:
    cfg = _config(root)
    if "layers" in cfg:
        return
    cfg["layers"] = 3
    path = root / "_brain" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
