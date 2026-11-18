#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Creates a fresh Cortex vault from template-vault/: scaffold, engine, config, hook, empty _inbox/, git.

    python3 scripts/new_vault.py ~/vaults/my-cortex --name "My Cortex" --agent "the assistant" --owner "the owner"
    python3 scripts/new_vault.py /tmp/fern --from examples/fern-cortex          # a runnable copy of the demo

Options:
  --name NAME          vault name stored in _brain/config.json (default: the folder name)
  --agent / --owner / --curator   names written into AGENTS.md (defaults: "the agent", "the owner", "the curator agent")
  --learner ID         default --by of `brain.py learn` (config "learner")
  --sibling NAME=PATH  a sibling vault, PATH relative to the new vault (repeatable)
  --from DIR           overlay a content folder (an example vault) on top of the template
  --git-name / --git-email   local git identity for this vault (otherwise your global identity is used)
  --no-git             only write files
Standard library only; Python 3.10+.
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "template-vault"
SKIP = {"state", "generated", "__pycache__", ".git"}  # never copied: runtime state, generated output, bytecode


def copy_tree(src: Path, dst: Path) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith((".pyc", ".tmp")) or f in ("Panel.html", "Panel.md", "proposals.md"):
                continue
            shutil.copy2(Path(root) / f, dst / rel / f)


def git(dest: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(dest), *args], check=check, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a fresh Cortex vault")
    ap.add_argument("dest")
    ap.add_argument("--name")
    ap.add_argument("--agent", default="the agent")
    ap.add_argument("--owner", default="the owner")
    ap.add_argument("--curator", default="the curator agent")
    ap.add_argument("--learner")
    ap.add_argument("--sibling", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--from", dest="overlay")
    ap.add_argument("--git-name")
    ap.add_argument("--git-email")
    ap.add_argument("--no-git", action="store_true")
    a = ap.parse_args()

    if sys.version_info < (3, 10):
        sys.exit("Python 3.10+ is required")
    dest = Path(a.dest).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"{dest} exists and is not empty")
    copy_tree(TEMPLATE, dest)
    if a.overlay:
        src = Path(a.overlay).expanduser().resolve()
        if not src.is_dir():
            sys.exit(f"--from {src}: not a folder")
        copy_tree(src, dest)
    copy_tree(REPO / "skills", dest / ".claude" / "skills")  # the curator's Claude Code skills, project-scoped

    cfg_path = dest / "_brain" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if a.name or "{{" in cfg["vault"]:
        cfg["vault"] = a.name or dest.name
    for item in a.sibling:
        name, _, path = item.partition("=")
        if not name or not path:
            sys.exit(f"--sibling {item}: use NAME=PATH")
        cfg.setdefault("siblings", {})[name] = path
    if a.learner:
        cfg["learner"] = a.learner
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    agents = dest / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    for key, value in (("VAULT", cfg["vault"]), ("AGENT", a.agent), ("OWNER", a.owner), ("CURATOR", a.curator)):
        text = text.replace("{{" + key + "}}", value)
    agents.write_text(text, encoding="utf-8")

    for hook in (dest / ".githooks").iterdir():
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    brain = [sys.executable, str(dest / "_brain" / "scripts" / "brain.py")]
    print(f"vault {cfg['vault']} at {dest}", flush=True)

    if a.no_git:
        subprocess.run(brain + ["build"], check=True)
        print("no git: run `git init -b main && git config core.hooksPath .githooks` before the first commit")
        return 0
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "core.hooksPath", ".githooks")
    probe = dest / ".cortex-mode-probe"   # a folder shared from Windows into a container shows every file executable:
    probe.write_text("", encoding="utf-8")   # git must not record that as the files' mode
    if os.access(probe, os.X_OK):
        git(dest, "config", "core.fileMode", "false")
    probe.unlink()
    if a.git_name:
        git(dest, "config", "user.name", a.git_name)
    if a.git_email:
        git(dest, "config", "user.email", a.git_email)
    subprocess.run(brain + ["build"], check=True)
    if not git(dest, "config", "user.email", check=False).stdout.strip():
        print("no git identity: set one (git config user.name/user.email) and commit with `git add -A && git commit -m \"config: new vault\"`")
        return 0
    git(dest, "add", "-A")
    r = git(dest, "commit", "-q", "-m", "config: new vault from the Cortex template", check=False)
    print(r.stdout + r.stderr if r.returncode else "first commit done (the pre-commit hook validated and built the vault)", flush=True)
    subprocess.run(brain + ["doctor"])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
