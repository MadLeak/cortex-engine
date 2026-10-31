#!/usr/bin/env python3
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
"""Wires an agent to a Cortex vault: one marked block in the agent's global instruction file (and, for Hermes Agent,
a skill). Idempotent: re-running replaces the block; --remove takes it out. Every changed file is backed up first.

    python3 installers/wire_agent.py --agent claude-code --vault ~/vaults/my-cortex
    python3 installers/wire_agent.py --agent codex --vault ~/vaults/my-cortex --role curator --dry-run
    python3 installers/wire_agent.py --agent hermes --vault ~/vaults/hermes-cortex --by hermes
    python3 installers/wire_agent.py --agent custom --target ~/some/agent/INSTRUCTIONS.md --vault ...

Global instruction files (verified against each tool's documentation, 2026-10-30; see docs/RUNBOOK.md):
  claude-code  ~/.claude/CLAUDE.md
  codex        $CODEX_HOME/AGENTS.md (CODEX_HOME defaults to ~/.codex)
  gemini       ~/.gemini/GEMINI.md
  opencode     ~/.config/opencode/AGENTS.md
  hermes       $HERMES_HOME/SOUL.md (HERMES_HOME defaults to ~/.hermes) + $HERMES_HOME/skills/<slug>/SKILL.md
Run it yourself: an agent should not rewrite another agent's configuration. Standard library only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def targets(agent: str) -> Path | None:
    home = Path.home()
    return {
        "claude-code": home / ".claude" / "CLAUDE.md",
        "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex") / "AGENTS.md",
        "gemini": home / ".gemini" / "GEMINI.md",
        "opencode": home / ".config" / "opencode" / "AGENTS.md",
        "hermes": Path(os.environ.get("HERMES_HOME") or home / ".hermes") / "SOUL.md",
    }.get(agent)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "cortex"


def guide(name: str, b: str, by: str, role: str) -> str:
    """How to use the vault: the same text goes in a block or a skill."""
    learn = (f'{b} learn --up "<exact L2 concept title>" --title "<short English title>" '
             f'--summary "<one English line, max 140 chars>" --type <fact|procedure|error|decision|example> '
             f'--content "<what is true>" --evidence "<how you know it: file, command, date>" --by {by}')
    text = f"""## Long-term memory: {name}
`{name}` is your graph memory (a Cortex vault). Use it for what you would otherwise guess: names, paths, past decisions, procedures, lessons. Skip it when the conversation already holds the answer or one look at the live system gives it.

How, quietly and cheaply:
1. `{b} recall "<English keywords>"`: signatures, and the body itself under `# answer:` when one node clearly answers.
2. Otherwise `{b} read "<exact title>"`: one node is usually enough, never more than two.
3. Stop when you can answer, after 2 useless reads, or on exit code 2 (budget spent).
4. `{b} credit --used "<title>"` with the nodes that helped. It is bookkeeping: do not mention it.
- `MAP.md` in the vault lists the Hot nodes: when a summary there answers, answer from it and credit it.
- `truth: current` = verify anything measurable before stating it; `historical` = a past record; `superseded` = wrong now. The live system wins over any node.

Learn: when you verify something reusable the vault lacks or gets wrong, store one fact per call:
`{learn}`
- If a node is wrong, do not edit it: learn the correct fact with `--contradicts "<title>"`.
- A learning starts provisional and becomes a seed after it helps in 3 sessions. Exit 1 = fix what the engine says; exit 2 = too many pending, write to the vault's `_inbox/` instead.
"""
    if role == "curator":
        text += f"""
Curate (you are this vault's curator): weekly `{b} consolidate`, then work through `_brain/proposals.md`: place `_inbox/`, decide conflicts, write reasons for Hebbian synapses with `{b} link`, sample confirmed learnings, commit with `learning:` or `consolidation:`. Never mark a node consolidated: only the human owner does.
"""
    else:
        text += """
Never create, edit, move or link other nodes, never run new, link, status, move, rename, consolidate or build, never edit MAP.md, _brain/ or git: the curator does that.
"""
    text += "Never store secrets, credentials or personal data in the vault, and never read it with generic file tools instead of recall and read.\n"
    return text


def markers(name: str) -> tuple[str, str]:
    return f"<!-- cortex:{slug(name)} -->", f"<!-- /cortex:{slug(name)} -->"


def blocks(agent: str, name: str, b: str, by: str, role: str) -> tuple[str, str | None]:
    """The marked block for the agent's instruction file and, for Hermes Agent, the skill the block points at."""
    marker, end = markers(name)
    if agent != "hermes":
        return f"{marker}\n{guide(name, b, by, role).rstrip()}\n{end}", None
    skill = (f"---\nname: {slug(name)}\ndescription: Long-term memory ({name}) - procedures, environment, past work "
             f"and lessons. Use it when a task depends on them and neither the conversation nor one look at the live "
             f"system answers, and to store a verified learning it lacks.\n---\n\n# {name}\n\n" + guide(name, b, by, role))
    block = (f"{marker}\n## Long-term memory: {name}\nYour `{slug(name)}` skill is your long-term memory. Use it when "
             "the answer is not already in the conversation or one look at the live system away; answer in your own "
             "voice, never narrate the lookup. The live system wins over any node.\n" + end)
    return block, skill


def main() -> int:
    ap = argparse.ArgumentParser(description="Wire an agent to a Cortex vault")
    ap.add_argument("--agent", required=True, choices=["claude-code", "codex", "gemini", "opencode", "hermes", "custom"])
    ap.add_argument("--vault", required=True, help="path of the vault")
    ap.add_argument("--target", help="instruction file to edit (required for custom; overrides the default)")
    ap.add_argument("--role", choices=["reader", "curator"], default="reader")
    ap.add_argument("--by", help="the agent's short name for `learn --by` (default: config learner or the agent)")
    ap.add_argument("--python", default="python3", help="interpreter the agent calls (python3, python, py)")
    ap.add_argument("--remove", action="store_true", help="remove the block (and the Hermes skill)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    vault = Path(a.vault).expanduser().resolve()
    brain = vault / "_brain" / "scripts" / "brain.py"
    if not brain.is_file():
        sys.exit(f"no _brain/scripts/brain.py in {vault}")
    cfg = json.loads((vault / "_brain" / "config.json").read_text(encoding="utf-8"))
    name = cfg.get("vault") or vault.name
    by = a.by or cfg.get("learner") or slug(a.agent)
    target = Path(a.target).expanduser() if a.target else targets(a.agent)
    if target is None:
        sys.exit("--target is required for --agent custom")
    if not a.remove:
        doctor = subprocess.run([sys.executable, str(brain), "doctor"], capture_output=True, text=True)
        print((doctor.stdout.strip().splitlines() or ["(no doctor output)"])[-1])

    b = f'{a.python} "{brain}"'
    marker, end = markers(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    def backup(path: Path) -> None:
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))

    block, skill = blocks(a.agent, name, b, by, a.role)
    skill_dir = target.parent / "skills" / slug(name) if skill else None

    text = target.read_text(encoding="utf-8") if target.exists() else ""
    s, e = text.find(marker), text.find(end)
    if a.remove:
        new = (text[:s].rstrip() + "\n" + text[e + len(end):].lstrip("\n")) if s >= 0 and e > s else text
    elif s >= 0 and e > s:
        new = text[:s] + block + text[e + len(end):]
    elif s >= 0:
        sys.exit(f"{target} has the opening marker but no closing one; fix it by hand")
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"

    if a.dry_run:
        print(f"would {'update' if new != text else 'leave'} {target}")
        if skill_dir:
            print(f"would {'remove' if a.remove else 'write'} {skill_dir / 'SKILL.md'}")
        print("\n" + (block if not a.remove else "(block removed)"))
        return 0
    if new != text:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup(target)
        target.write_text(new, encoding="utf-8")
        print(f"{'removed from' if a.remove else 'wrote'} {target}" + (" (backup beside it)" if text else ""))
    else:
        print(f"{target} already up to date")
    if skill_dir:
        if a.remove:
            shutil.rmtree(skill_dir, ignore_errors=True)
            print(f"removed {skill_dir}")
        else:
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(skill, encoding="utf-8")
            print(f"skill   {skill_dir / 'SKILL.md'}")
    print("Restart the agent so it reloads its instructions. Test: ask it something only the vault knows, without naming the vault.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
