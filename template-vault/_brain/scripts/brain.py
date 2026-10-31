# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Cortex engine entry point: `python _brain/scripts/brain.py <command>`.
The engine lives in brain_core.py. A script run directly is recompiled on every call, an imported module is not
(Python caches its bytecode), so this file stays tiny and imports the engine."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# agent shells often set PYTHONDONTWRITEBYTECODE; without the cached bytecode every call recompiles the engine
# (~0.13 s on Windows). __pycache__ is gitignored, so the cache is written whatever the environment says.
sys.dont_write_bytecode = False
import brain_core  # noqa: E402

if __name__ == "__main__":
    sys.exit(brain_core.main())
sys.modules[__name__] = brain_core  # `import brain` (tests, tools) gets the engine itself
