# -*- coding: utf-8 -*-
"""The engine's output on the vaults inside this repository, byte for byte, against the digest taken before the layers
became roles (golden_layers.json). A change that is meant to alter output (a new rule, a new column) regenerates the
digest on purpose: `python tests/golden_layers.py digest > tests/golden_layers.json`, and the diff of that file is
the review. Skipped where the example vaults are not beside the engine (an installed vault)."""
import json
import unittest
from pathlib import Path

import golden_layers as g

GOLDEN = Path(__file__).resolve().parent / "golden_layers.json"


@unittest.skipUnless((g.REPO / "examples").is_dir(), "the example vaults are not beside this engine")
class GoldenTests(unittest.TestCase):
    def test_every_command_prints_what_it_printed_before(self):
        want, got = json.loads(GOLDEN.read_text(encoding="utf-8")), g.digest()
        self.assertEqual(sorted(want), sorted(got))
        changed = [f"{v}: {k}" for v in want for k in sorted(set(want[v]) | set(got[v])) if want[v].get(k) != got[v].get(k)]
        self.assertEqual(changed, [], "output changed (see tests/golden_layers.py capture to diff by hand)")


if __name__ == "__main__":
    unittest.main()
