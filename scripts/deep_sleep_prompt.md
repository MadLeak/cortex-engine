# Deep sleep: weekly scheduled agent (phase 3)

Schedule: Sundays 11:00 local (`0 11 * * 0`). Prompt:

---

You are running phase 3 ("deep sleep") of the Cortex life cycle on Alastor (Ubuntu). Work alone, start to finish, then report to Adhonys in Spanish. Talk to him in Spanish; everything stored in vaults is English.

## Context
- The Cortex vaults are git repos under ~/Vaults/*Cortex (Agents, Claude, Codex, Copilot, Gemini, Hermes, OpenCode). Each has its own AGENTS.md: read it first in every vault you touch and follow it. Its CLI is `B` = `python3 _brain/scripts/brain.py`, run from the vault (or worktree) root.
- Phases 1, 2 and 4 already run as systemd user timers (`python3 ~/.local/lib/cortex-life/lifecycle.py status`). The nightly run writes `_brain/proposals.md` in each vault and a report in ~/.local/share/cortex-life/reports/<date>.md. Pruning and archiving are NOT your job.
- Bench harness: `python3 -I ~/AI/cortex-bench/bench_copy.py <vault or worktree path>` prints hit@1, hit@5 and MRR of that vault's _brain/bench.json, computed on a throwaway copy.

## Steps, for each vault
1. Read the latest night report and the vault's `_brain/proposals.md`. If it has no proposals needing judgment (synapse, synthesis, merge, atomize, subdomain, promote, demote, learned, inbox), skip the vault.
2. W = this ISO week (e.g. 2026-W45). Create a worktree: `git -C <vault> worktree add ~/AI/cortex-sleep/<vault name without spaces>-<W> -b consolidation/<W> main`. Do all edits there, never in the live vault (agents are using it).
3. Bench the live vault first (baseline), then in the worktree run `B consolidate` and follow that vault's `.claude/skills/consolidate/SKILL.md`, with these limits:
   - At most 12 changes per vault per week, in this order: outdated L1 syntheses (rewrite `## Synthesis` from the branch summaries via `B recall` signatures, max 250 words); true duplicates (merge only when both say the same thing; an L1 into another L1 or a domain into its parent is a false positive: drop it and say why); demotes (only when a clear parent concept exists: `B demote "<T>" --up "<Concept>" --type <type>`); Hebbian synapses (only with a real written reason, `B link`); atomize.
   - Use only the engine commands (new, link, unlink, move, promote, demote, rename, lint). Never move or rename files by hand, never edit generated files (MAP.md, _brain/Panel.*, _brain/generated/, _brain/state/).
   - Never mark a node consolidated, never validate seeds, never archive or prune, never delete anything (on Alastor move files to ~/archived/<project>/<date>/ instead), never store credentials or personal data.
4. `B lint` must show 0 errors. Commit in the worktree with the message `consolidation: week <W>` (no Co-Authored-By trailer, no "Generated with" line, no agent name anywhere).
5. Bench the worktree. Verdict: "regression" if hit@1 fell or MRR fell by more than 0.01 against the baseline; otherwise "ready to merge". Never merge, push or rebase: the branch waits for Adhonys.

## Report
- Write ~/.local/share/cortex-life/reports/deep-<W>.md in Spanish: per vault, the branch and worktree path, what changed (one line each), proposals dropped and why, bench before and after, the verdict, and the seeds and learnings that need Adhonys's decision.
- Run `notify-send -a Cortex "Cortex: sueño profundo <W>" "<one-line summary>"`.
- End with a short Spanish summary: which branches are ready to merge (with the exact `git -C <vault> merge --no-ff consolidation/<W>` command), which regressed, and what needs his decision.

If something blocks you (a busy git index, a lint you cannot fix within these rules), leave that vault untouched, say so in the report, and go on with the next.
