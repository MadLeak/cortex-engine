#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Every test suite of the engine and the hub, each in its own process: `docker compose run --rm test`.

The engine's suite runs from its own folder (it imports brain.py beside it); the hub and MCP suites run from the
repository root. Exit code 1 when any suite fails."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = [("engine", ROOT / "template-vault/_brain/scripts", ["discover", "-s", "tests"])]
SUITES += [(t.stem, ROOT, [str(t.relative_to(ROOT))]) for t in sorted((ROOT / "scripts").glob("test_*.py"))]


def main() -> int:
    failed = []
    for name, cwd, args in SUITES:
        print(f"== {name}", flush=True)
        if subprocess.run([sys.executable, "-m", "unittest", *args], cwd=cwd).returncode:
            failed.append(name)
    print(f"\n{len(SUITES) - len(failed)} of {len(SUITES)} suites passed" + (f" · failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
