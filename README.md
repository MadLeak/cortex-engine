# Cortex

**A graph second brain for AI agents.** Cortex gives an agent a long-term memory that stays small, auditable and owned by a human: plain Markdown notes in a git repository, organised in layers, read through a budgeted funnel, and changed only through commits a pre-commit hook has validated.

- **Plain files.** Every memory is a Markdown note with YAML frontmatter. The vault opens in [Obsidian](https://obsidian.md) or any editor, and git keeps its history.
- **Layers.** L1 domains, L2 concepts and L3 details, each with a word limit. A generated `MAP.md` is the always-loaded core (L0).
- **Budgeted reading.** `recall` returns one-line signatures (and the body itself when one answer is clearly ahead), `read` hands over at most a few bodies, and exit code 2 tells the agent to stop and answer.
- **Use shapes weight.** Reads and credits feed a decayed weight, an ACT-R "Hot" list in `MAP.md`, and Hebbian proposals for new links.
- **Agents learn, humans decide.** An agent adds a *provisional* fact with `learn`; use confirms it; a curator agent tidies; only the human marks a node *consolidated*.
- **No dependencies.** Python 3.10+ standard library only. One ~3,000-line engine file.

Read the [design paper](docs/PAPER.md) for the why and the [runbook](docs/RUNBOOK.md) for day-to-day operation.

## 60-second quickstart

Requirements: Python 3.10 or newer (the engine's `doctor` checks for 3.10; the test suite was run on Python 3.14.4), git, and optionally Obsidian.

```bash
git clone <this repository> cortex && cd cortex

# 1. Create a vault (scaffold, engine, config, hook, git repo, first commit)
python3 scripts/new_vault.py ~/vaults/my-cortex --name "My Cortex" --agent "my agent" --owner "me"
cd ~/vaults/my-cortex
B="python3 _brain/scripts/brain.py"
$B doctor                                   # every line should be ✔

# 2. Add nodes: a domain, a concept under it, a detail under that
$B new --layer 1 --title "Deployments" --summary "How and where the team's services are deployed."
$B new --layer 2 --up "Deployments" --title "Deploys happen on Tuesdays" \
       --summary "Production deploys run only on Tuesday mornings, after the weekly review."
$B new --layer 3 --up "Deploys happen on Tuesdays" --type decision \
       --title "Friday deploys were stopped in March" --summary "Decision: two Friday incidents ended Friday deploys."
# edit the bodies in your editor, then link two nodes with a reason:
$B link "Friday deploys were stopped in March" "Deployments" --type example_of --reason "the incident that set the rule"
git add -A && git commit -m "learning: deployment rules"   # the hook lints and rebuilds MAP and the panel

# 3. Read like an agent
$B recall "which day do deploys happen"     # signatures (and the body if one answer is clear)
$B read "Deploys happen on Tuesdays"        # a body, counted against the budget
$B credit --used "Deploys happen on Tuesdays"

# 4. Open the panel
xdg-open _brain/Panel.html                  # macOS: open · Windows: start
```

Try the demo instead: `python3 scripts/new_vault.py /tmp/demo/kitchen-cortex --from examples/kitchen-cortex` and then `python3 scripts/new_vault.py /tmp/demo/fern-cortex --from examples/fern-cortex`. That gives a 16-node gardening vault with a sibling vault, typed links, truth tags and one provisional learning. Then `cd /tmp/demo/fern-cortex && python3 _brain/scripts/brain.py recall "when should the beds be watered"`.

## Wire an agent

```bash
python3 installers/wire_agent.py --agent claude-code --vault ~/vaults/my-cortex   # or codex, gemini, opencode, hermes, custom
```

This writes one marked, idempotent block into the agent's global instruction file and backs the file up first. Hermes Agent also gets a skill. The paths it writes are listed in the [runbook](docs/RUNBOOK.md#4-wire-an-agent), with the documentation each was checked against.

## Repository layout

| Path | What |
|---|---|
| `template-vault/` | A complete empty vault: `_brain/scripts/` (the engine and its tests), `_templates/`, `.githooks/pre-commit`, `.obsidian/`, placeholder `AGENTS.md` / `CLAUDE.md`, empty `_inbox/` |
| `scripts/new_vault.py` | Creates a vault from the template, with an optional content overlay (`--from`) |
| `scripts/upgrade_engine.py` | Installs this engine into existing vaults and runs their `doctor` |
| `installers/wire_agent.py` | Wires Claude Code, Codex, Gemini CLI, OpenCode, Hermes Agent or any agent to a vault |
| `skills/` | Claude Code skills for the curator: capture, atomize, link, review, consolidate (copied into each new vault's `.claude/skills/`) |
| `examples/` | Two fictional sibling vaults: `fern-cortex` (a gardening agent) and `kitchen-cortex` (a kitchen agent) |
| `docs/` | [RUNBOOK.md](docs/RUNBOOK.md) (operations) and [PAPER.md](docs/PAPER.md) (design) |

## Tests

```bash
cd template-vault/_brain/scripts && python3 -m unittest discover -s tests -v
```

The suite has 31 tests, including a 1,000-node performance check, and runs in about 3 seconds.

## Status

Engine version 2.5.3. Cortex comes from one person's working setup of several agents sharing a machine. It works, but it is young: see *Limitations* in the paper.

## License

**TODO:** the owner has not chosen a license yet. Until a `LICENSE` file is added, all rights are reserved by default.
