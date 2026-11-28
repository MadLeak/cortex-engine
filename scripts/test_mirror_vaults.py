# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""The read-only copy only ever fast-forwards: new remote commits come in, local work is never touched."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mirror_vaults  # noqa: E402


def run(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.remote, self.server, self.copy = t / "remote.git", t / "server", t / "copy" / "Kale Cortex"
        run(t, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        run(t, "clone", "--quiet", str(self.remote), str(self.server))
        (self.server / "_brain").mkdir()
        self.commit(self.server, "a.md", "one")
        run(self.server, "push", "--quiet", "origin", "main")
        run(t, "clone", "--quiet", str(self.remote), str(self.copy))

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        run(repo, "add", "-A")
        run(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", name)

    def test_new_remote_commits_are_fast_forwarded(self):
        self.assertIsNone(mirror_vaults.mirror(self.copy), "already current: nothing to say")
        self.commit(self.server, "b.md", "two")
        run(self.server, "push", "--quiet", "origin", "main")
        self.assertEqual(mirror_vaults.mirror(self.copy), "pulled 1 commit(s)")
        self.assertEqual((self.copy / "b.md").read_text(), "two")

    def test_local_changes_and_commits_are_left_alone(self):
        (self.copy / "a.md").write_text("edited here")
        self.assertIn("local changes", mirror_vaults.mirror(self.copy))
        self.assertEqual((self.copy / "a.md").read_text(), "edited here")
        run(self.copy, "checkout", "--quiet", "a.md")
        (self.copy / "_brain").mkdir(exist_ok=True)
        self.commit(self.copy, "c.md", "mine")
        self.assertIn("1 local commit", mirror_vaults.mirror(self.copy))
        self.assertTrue((self.copy / "c.md").is_file())


if __name__ == "__main__":
    unittest.main()
