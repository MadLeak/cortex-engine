# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Migration 0002: the push token stays out of git.

The hub's Settings keep a vault's push token in _brain/state/remote.token (engine 2.19): its .gitignore line.
Frozen: what schema 2 means. Contract: see m0001_baseline.py.
"""
from pathlib import Path

ID = 2
TITLE = "the push token stays out of git"
LINE = "_brain/state/remote.token"


def _have(root: Path) -> bool:
    path = root / ".gitignore"
    return path.exists() and LINE in {x.strip() for x in path.read_text(encoding="utf-8").splitlines()}


def plan(root: Path) -> list[str]:
    return [] if _have(root) else [".gitignore"]


def up(root: Path) -> None:
    if _have(root):
        return
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "# The push token stays out of git (migration 0002)\n" + LINE + "\n", encoding="utf-8")
