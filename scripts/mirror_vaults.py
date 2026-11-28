# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Keep a read-only copy of the vaults a remote hub serves: fetch each one and fast-forward it to its remote.

For a machine whose agents use a hub on a server over MCP while local tools (session hooks, Obsidian) still read the
notes from disk. The server's sync pushes what the agents learn; this brings it here. Nothing is ever written to a
vault but a fast-forward: a vault with local changes or local commits is left alone and reported, never reset.

  python mirror_vaults.py --root ~/Vaults            # once
  schedule it every 10 minutes (Windows: pythonw, so no window opens)
A line per vault that changed or was skipped goes to <root>/.cortex-mirror.log (the last 500 lines are kept).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: git opens no console under pythonw
KEEP = 500


def git(v: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(v), *args], capture_output=True, text=True, timeout=120,
                          creationflags=NO_WINDOW)


def mirror(v: Path) -> str | None:
    """Fast-forwards one vault. A line to log when something happened or was refused; None when already current."""
    if git(v, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "skipped: local changes (this copy is read-only; the server holds the truth)"
    if git(v, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
        return "skipped: no upstream branch"
    f = git(v, "fetch", "--quiet")
    if f.returncode != 0:
        return "fetch failed: " + (f.stderr.strip().splitlines() or ["?"])[-1]
    ahead, behind = (int(x) for x in git(v, "rev-list", "--left-right", "--count", "HEAD...@{u}").stdout.split())
    if ahead:
        return f"skipped: {ahead} local commit(s) not on the remote"
    if not behind:
        return None
    m = git(v, "merge", "--ff-only", "--quiet", "@{u}")
    return f"pulled {behind} commit(s)" if m.returncode == 0 else "fast-forward failed: " + m.stderr.strip()[-200:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path.home() / "Vaults")
    a = ap.parse_args()
    root = a.root.expanduser()
    lines = []
    for v in sorted(p for p in root.iterdir() if (p / ".git").exists() and (p / "_brain").is_dir()):
        try:
            msg = mirror(v)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            msg = f"error: {e}"
        if msg:
            lines.append(f"{datetime.now().isoformat(timespec='seconds')} {v.name}: {msg}")
    if lines:
        log = root / ".cortex-mirror.log"
        old = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        log.write_text("\n".join((old + lines)[-KEEP:]) + "\n", encoding="utf-8")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
