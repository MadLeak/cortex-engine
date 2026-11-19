# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Migration 0001: the baseline every vault reaches before schema 1.

The engine's own state and the MCP token stay out of git: the .gitignore lines a vault made before engine 2.17 may
lack (the token line came with the MCP service; older vaults miss others too). Frozen: this list is what schema 1
means, whatever the template says later; a later change is a later migration.

Contract of a migration (see brain_core.cmd_migrate): ID, TITLE, plan(root) -> the vault-relative paths up() would
write ([] when there is nothing to do), and up(root), which writes only those paths. Deterministic and idempotent:
the same vault gives the same result, and a second run changes nothing.
"""
from pathlib import Path

ID = 1
TITLE = "baseline: engine state and the MCP token stay out of git"

IGNORE = ("_brain/state/sessions/", "_brain/state/usage.jsonl", "_brain/state/.lock", "_brain/state/cache.json",
          "_brain/state/activity.js", "_brain/state/mcp.token", "_brain/state/*.pickle", "*.tmp", "__pycache__/")


def _missing(root: Path) -> list[str]:
    path = root / ".gitignore"
    have = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()} if path.exists() else set()
    return [line for line in IGNORE if line not in have]


def plan(root: Path) -> list[str]:
    return [".gitignore"] if _missing(root) else []


def up(root: Path) -> None:
    missing = _missing(root)
    if not missing:
        return
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += ("\n" if text else "") + "# Engine state and the MCP token stay out of git (migration 0001)\n"
    path.write_text(text + "\n".join(missing) + "\n", encoding="utf-8")
