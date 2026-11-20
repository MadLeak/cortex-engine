# Cortex

*By MadLeak · Adhonys Diaz*

**A graph second brain for AI agents.** Cortex gives an agent a long-term memory that stays small, auditable and owned by a human: plain Markdown notes in a git repository, organised in layers, read through a budgeted funnel, and changed only through commits a pre-commit hook has validated.

- **Plain files.** Every memory is a Markdown note with YAML frontmatter. The vault opens in [Obsidian](https://obsidian.md) or any editor, and git keeps its history.
- **Layers.** L1 domains, L2 concepts and L3 details, each with a word limit. A generated `MAP.md` is the always-loaded core (L0).
- **Budgeted reading.** `recall` returns one-line signatures (and the body itself when one answer is clearly ahead), `read` hands over at most a few bodies, and exit code 2 tells the agent to stop and answer.
- **Use shapes weight.** Reads and credits feed a decayed weight, an ACT-R "Hot" list in `MAP.md`, and Hebbian proposals for new links.
- **Agents learn, humans decide.** An agent adds a *provisional* fact with `learn`; use confirms it; a curator agent tidies; only the human marks a node *consolidated*.
- **No dependencies.** Python 3.10+ standard library only. One ~3,000-line engine file.

Read the [design paper](docs/PAPER.md) for the why and the [runbook](docs/RUNBOOK.md) for day-to-day operation.

## Quickstart with Docker alone

Requirements: Docker (Docker Desktop on Windows and macOS). Nothing else on the host: no Python, no administrator, no DNS
or certificate changes. The engine, the panels, the MCP service and the command line all run in one container; the
vaults stay plain folders under `~/Vaults` (`%USERPROFILE%\Vaults` on Windows), so Obsidian sees them as before.

```bash
git clone <this repository> cortex && cd cortex
./install.sh                        # Windows: powershell -ExecutionPolicy Bypass -File install.ps1
```

Then open `http://cortex.localhost:8790` (browsers resolve `*.localhost` to this machine by themselves). *Connect an
agent* creates a cortex and gives the one line its MCP client needs, for example:

```bash
claude mcp add --transport http --scope user claude-cortex "http://cortex.localhost:8790/mcp/Claude%20Cortex?by=claude-code" --header "Authorization: Bearer <the vault's token>"
```

A new cortex can start with the agent's Claude Code memory: tick its projects in the form, and the memories are imported
into the graph by rule (no model). The agent starts each task with `action=brief` and ends it with `action=finish`, which commits what it learned, signed with
your `CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL` (the agent's name goes in the message; with none set, it signs as itself). The command line is `./cortex.sh` (Windows `.\cortex.ps1`): `list`, `new <folder> [--from
kitchen-cortex]`, `brain <vault> recall "..."`, `doctor`, `upgrade`, `rem <vault>`, `ideas <vault>`, `token <vault> [--rotate]`, `commit <vault> -m`.
Options: `--lan` / `-Lan` serves the cortexes you mark LAN to the local network over HTTPS; `--trusted` / `-Trusted`
(administrator) gives `https://cortex.local` with no warning by trusting the hub's CA on this machine. See the
[runbook](docs/RUNBOOK.md#1-install).

## 60-second quickstart with Python on the host

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

Several agents? `./install.sh` (one command, one password) serves every vault under `~/Vaults` on one HTTPS page, localhost or local network only, with its own constrained CA: a card per agent, a live wall of their panels, and a form that creates a cortex for a new agent and gives you the prompt that wires it (see the runbook, section 8).

Try the demo instead: `python3 scripts/new_vault.py /tmp/demo/kitchen-cortex --from examples/kitchen-cortex` and then `python3 scripts/new_vault.py /tmp/demo/fern-cortex --from examples/fern-cortex`. That gives a 16-node gardening vault with a sibling vault, typed links, truth tags and one provisional learning. Then `cd /tmp/demo/fern-cortex && python3 _brain/scripts/brain.py recall "when should the beds be watered"`.

## Privacy: a vault is never public

The engine is shareable; a vault built with it is not. Each agent atomizes what it learns about its person into linked notes, so a published graph exposes far more personal information than any single note. Never push a vault to a public repository or share its notes, graph or panels on the internet.

The vault's `pre-push` hook enforces this: before every push it asks the forge (GitHub, GitLab, Codeberg, Bitbucket) without credentials whether the repository is readable by anyone, and refuses the push if it is, or if it cannot get an answer (`CORTEX_ALLOW_UNVERIFIED_PUSH=1` overrides that last case once you have checked yourself). `doctor` reports any public remote. A self-hosted remote passes with a reminder to keep it private.

## Wire an agent

With the container, an agent connects over MCP from the hub's *Connect an agent* form (above): no file of the agent's
is written. With Python on the host:

```bash
python3 installers/wire_agent.py --agent claude-code --vault ~/vaults/my-cortex   # or codex, gemini, opencode, hermes, custom
```

This writes one marked, idempotent block into the agent's global instruction file and backs the file up first. Hermes Agent also gets a skill. The paths it writes are listed in the [runbook](docs/RUNBOOK.md#4-wire-an-agent), with the documentation each was checked against.

## Repository layout

| Path | What |
|---|---|
| `template-vault/` | A complete empty vault: `_brain/scripts/` (the engine and its tests), `_templates/`, `.githooks/pre-commit` and `pre-push`, `.obsidian/`, placeholder `AGENTS.md` / `CLAUDE.md`, empty `_inbox/` |
| `compose.yaml`, `Dockerfile` | The Cortex container: hub, MCP service and engine; `compose.lan.yaml` and `compose.trusted.yaml` add the LAN and host-trusted modes; the `tools` profile has `cli`, `test` and `bench` |
| `install.sh`, `install.ps1`, `cortex.sh`, `cortex.ps1` | Install with Docker alone; the command line in the container (`scripts/cortex_cli.py`) |
| `scripts/cortex_hub.py`, `scripts/cortex_mcp.py` | The hub (panels, *Connect an agent*) and the MCP service it serves at `/mcp/<vault>`: one token per vault, a resident engine, MCP 2025-11-25 and 2026-07-28, the Skills extension |
| `scripts/new_vault.py` | Creates a vault from the template, with an optional content overlay (`--from`) |
| `scripts/upgrade_engine.py` | Installs this engine into existing vaults and runs their `doctor` |
| `installers/wire_agent.py` | Wires Claude Code, Codex, Gemini CLI, OpenCode, Hermes Agent or any agent to a vault |
| `skills/` | Claude Code skills for the curator: capture, atomize, link, review, consolidate (copied into each new vault's `.claude/skills/`) |
| `examples/` | Two fictional sibling vaults: `fern-cortex` (a gardening agent) and `kitchen-cortex` (a kitchen agent) |
| `docs/` | [RUNBOOK.md](docs/RUNBOOK.md) (operations), [DECISIONS.md](docs/DECISIONS.md) (what was decided and how to apply it) and [PAPER.md](docs/PAPER.md) (design) |

## Tests

```bash
docker compose --profile tools run --rm test     # every suite in the image: engine, hub, MCP, review
docker compose --profile tools run --rm bench    # what one engine call costs, new process against resident engine
cd template-vault/_brain/scripts && python3 -m unittest discover -s tests -v   # the engine alone, with Python
```

The engine's suite has 62 tests, including a 1,000-node performance check, and runs in about 5 seconds; the hub has 25 and the MCP service 91.

## Status

Engine version 2.26.0. Since 2.16 it runs in Docker only, with the resident engine keeping the graph, index and usage sums in memory (recall on a 1,000-node vault: 842 ms to 96 ms on a slow Docker Desktop bind mount, 28 ms to 23 ms on tmpfs), with REM dreaming (the questions each note answers, from your local model server) scheduled from the hub's Settings, with the layers expressed as roles in a table (domain, concept, leaf; the default is the paper's three), with an ideas miner (a plain pass that proposes concepts and links to the curator through the inbox), with an MCP service per vault, vault migrations with no model, Claude Code memory import, per-cortex *Settings* (config, remote, push token, MCP token, delete) on the hub and from each panel, and a panel whose recalls are drawn as lightning. Known gaps are listed in the [runbook](docs/RUNBOOK.md#known-gaps-not-verified-or-not-built). Cortex comes from one person's working setup of several agents sharing a machine. It works, but it is young: see *Limitations* in the paper.

## License

Copyright (c) 2026 Adhonys Diaz. Cortex is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md): you may use, study, modify and share it for any noncommercial purpose, including personal use, research, education and work by charities and public institutions.

Any commercial use (in a product, a paid service, or inside a for-profit company's operations) needs a separate commercial license from the copyright holder. To ask for one, open an issue titled "Commercial license".
