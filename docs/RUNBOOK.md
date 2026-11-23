# Cortex runbook

> Decisions behind the Docker-only design, and the rules for changing it: [DECISIONS.md](DECISIONS.md).

This is the operations manual for running Cortex vaults day to day. `B` below means `python3 _brain/scripts/brain.py`, run from the vault root. Use `python` or `py` where that is your interpreter. Every command has `-h`.

Contents: 1 Install · 2 Create a vault · 3 Git and the hook · 4 Wire an agent · 5 The daily loop · 6 The curator's week · 7 Sibling vaults · 8 The panel · 9 Backups · 10 Upgrading the engine · 11 Troubleshooting · 12 Reference

---

## 1. Install

**With Docker alone (recommended).** Docker is the only requirement on the host. `./install.sh` (Windows:
`install.ps1` in a normal PowerShell) writes `.env` from `.env.example` when there is none, creates `~/Vaults`, starts
the container and waits for the hub at `http://cortex.localhost:8790`. Nothing on the host changes: one port on
127.0.0.1, plain HTTP (browsers treat `localhost` as secure), names under `.localhost` that browsers resolve by
themselves. Options: `--lan` / `-Lan` (the cortexes marked LAN on the local network, HTTPS by the hub's own CA, each
device accepts it once), `--trusted` / `-Trusted` (administrator: `https://cortex.local` with no warning, §8),
`--remove` / `-Remove`.

- **Where the vaults live:** `CORTEX_VAULTS` in `.env`. Unset, the host folder `~/Vaults` (`%USERPROFILE%\Vaults`) is
  mapped at `/vaults`; a path maps that folder instead; the bare name `cortex-vaults` maps no host folder and keeps the
  vaults in the Docker volume `cortex_cortex-vaults` (no Obsidian or host git on them, and `--trusted` is refused because
  it installs the CA from the folder).
- **The `vaults` service** (`scripts/vaults_init.sh`) runs before the hub on every `up` and exits: it gives the vaults
  place to `CORTEX_UID:CORTEX_GID` (a new volume or a folder Docker made starts out as root's), and when that place holds
  no vault yet (at most `.cortex-hub`) while `CORTEX_VAULTS_FROM` has something, it copies everything over with `cp -a`.
  So switching is: new place in `CORTEX_VAULTS`, old place in `CORTEX_VAULTS_FROM` (unset means the `cortex-vaults`
  volume), `docker compose up -d`. A non-empty new place is never written to, and the old place is never deleted.
  It runs as root with only `CHOWN`, `DAC_OVERRIDE` and `FOWNER`, no network and a read-only root.
- **Careful:** `docker compose down -v` deletes the `cortex-vaults` volume and every vault in it; `down` keeps it.
- **No `.env` needed:** every setting has its default in `compose.yaml`, so `docker compose up -d --build` alone runs
  the hub. Set `CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL` anyway (without them nothing learned is committed), and on Linux
  `CORTEX_UID` / `CORTEX_GID` when your ids are not 1000. A missing `~/.cortex-secrets/push.token` or
  `~/.claude/projects` is made by Docker as an empty folder; the hub then has no shared push token or memories to import.

- **Command line:** `./cortex.sh <command>` (Windows `.\cortex.ps1`) runs `scripts/cortex_cli.py` in the container:
  `list`, `new`, `brain <vault> <engine args>`, `doctor`, `upgrade`, `rem <vault>`, `ideas <vault>`, `token <vault> [--rotate]`, `commit <vault> -m`
  (as `CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`), `precommit`.
- **Engine versions:** on start the hub upgrades every vault with an older engine and commits it as
  `Cortex hub <hub@cortex.local>`.
- **Commits from the host** (Obsidian, git on Windows): the vault's `pre-commit` hook uses the host's Python when there
  is one, and otherwise runs the same check in the `cortex-hub` image (`CORTEX_HOOK_DOCKER=1` forces it).
- **Windows folders:** a vault shared from Windows shows every file as executable inside the container; the container
  sets `core.fileMode false` on such vaults so neither git sees every file as changed.
- **Tests:** `docker compose --profile tools run --rm test`.

**With Python on the host.** Requirements:
- **Python 3.10 or newer.** `B doctor` fails below 3.10 and the hook refuses to run. The engine uses only the standard library. The tests were run on Python 3.14.4.
- **git.** Each vault is its own repository.
- **Obsidian** (optional), to browse and edit notes as a graph. `.obsidian/` in the template hides the engine folders and colours the layers.

Clone this repository anywhere. It is a toolkit, not a vault: vaults live elsewhere and carry their own copy of the engine in `_brain/scripts/`, so each one works without this repository.

Check the engine before you use it:

```bash
cd template-vault/_brain/scripts && python3 -m unittest discover -s tests
```

Expect `Ran 73 tests ... OK`, plus a timing line for the 1,000-node performance test.

## 2. Create a vault

```bash
python3 scripts/new_vault.py ~/vaults/fern-cortex \
    --name "Fern Cortex" --agent Fern --owner Sam --curator "the curator agent" \
    --learner fern --sibling "Kitchen Cortex=../kitchen-cortex"
```

What it does:
1. Copies `template-vault/` to the destination, without runtime state, generated files or bytecode. The destination must be empty or absent.
2. Copies `skills/` into the vault's `.claude/skills/` (Claude Code project skills for the curator).
3. Optional `--from DIR`: overlays a content folder, such as an example vault, on top.
4. Writes `_brain/config.json`: the vault name, siblings, and the `learner` default for `learn --by`. It fills the `{{VAULT}}`, `{{AGENT}}`, `{{OWNER}}` and `{{CURATOR}}` placeholders in `AGENTS.md`.
5. Makes the hook executable, runs `git init -b main`, sets `core.hooksPath .githooks`, builds, makes the first commit (through the hook) and runs `doctor`.

Without a git identity (no global one, and no `--git-name` / `--git-email`), it stops before the commit and says so. `--no-git` writes the files only.

**Naming.** One vault per agent. Name it after the agent ("Fern Cortex"). The name in `config.json` is how siblings and external references find it, so treat it as an identifier.

**What goes where.**

| File | Purpose | Who edits |
|---|---|---|
| `AGENTS.md` | L0 conventions every agent reads first | human or curator |
| `CLAUDE.md` | `@AGENTS.md` plus Claude Code specifics. `doctor` checks that it exists | human |
| `MAP.md` | L0 map: domains and the Hot list | generated |
| `_brain/config.json` | name, siblings, half-life, allowed addresses, learner | human |
| `_brain/glossary.json` | the vault's own Spanish→English query words (wins over the engine's lexicon) | curator |
| `_templates/` | bodies for L1, L2 and L3 nodes | rarely |
| `_inbox/` `_sources/` `_log/` `_archive/` | the cold ring: not searched by recall | curator |

Optional `config.json` keys: `half_life_days` (30), `confirm_sessions` (3), `confirm_min_days` (2), `max_provisional` (25), `learner`, `warn_dependency_on` (a list of sibling names to warn on), and `allowed_addresses` (`{"address": "why it is kept"}`, for email addresses kept on purpose).

## 3. Git and the hook

Every vault is a git repository with two hooks. The pre-commit hook (`.githooks/pre-commit`) does two things:
1. **Validates.** It runs `B precommit`, which is `lint`: errors block the commit. Errors include secrets, a missing summary, a summary over 140 characters, a broken synapse, a synapse with no reason, more than 7 synapses, a wrong parent layer, a duplicate title or id, and a cycle.
2. **Regenerates.** It rebuilds and stages `MAP.md`, `_brain/Panel.md`, `_brain/Panel.html`, `_brain/generated/` and `_brain/state/history.csv` (plus `_brain/proposals.md` if present).

The pre-push hook (`.githooks/pre-push`) runs `B prepush <remote> <url>`: **a vault is never published**, because its graph exposes what the agents learned about their person. It asks the forge (GitHub, GitLab, Codeberg, Bitbucket) without credentials whether the repository is readable by anyone and refuses the push if it is. If the forge does not answer, the push is refused too; `CORTEX_ALLOW_UNVERIFIED_PUSH=1 git push` overrides that case once you have checked the repository yourself. A self-hosted remote passes with a reminder. `B doctor` also flags any public remote.

Setup on a fresh clone of a vault:

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push   # git does not always keep the executable bit
git config user.name  "Your Name"      # local identity, if you have no global one
git config user.email "you@example.org"
B doctor
```

Commit prefixes used by the skills and the curator: `learning:` (new knowledge), `consolidation:` (curation), `manual:` (human captures), `engine:` (engine upgrades), `config:` (configuration).

Never skip the hook with `--no-verify`. It is the only gate that keeps secrets out of history.

`_brain/state/` holds sessions, the usage log and caches. Most of it is gitignored. `history.csv` and `hot.jsonl` are kept.

## 4. Wire an agent

**Over MCP (the container).** The hub serves MCP at `/mcp/<vault>` on its own port, each vault behind its own token
(`<vault>/_brain/state/mcp.token`, never committed; `./cortex.sh token <vault> --rotate` makes a new one and the old one
stops working). *Connect an agent* gives the chosen client's configuration: `claude mcp add --transport http ...` for
Claude Code, a `[mcp_servers.<name>]` block for Codex (`~/.codex/config.toml`), an `httpUrl` entry for Gemini CLI
(`~/.gemini/settings.json`), a `remote` entry for OpenCode, a generic `mcpServers` JSON, and a stdio bridge
(`docker exec -i cortex-hub-1 python3 /opt/cortex/scripts/cortex_mcp.py --vault /vaults/<vault> --by <agent>`) for any
client that only speaks stdio. `?by=<agent>` names the agent: its task sessions and its commits carry that name. Nothing
is written to the agent's instruction files: the `cortex` tool describes its own use, and over MCP 2026-07-28 the
Skills extension serves the curator skills. The server's `instructions` (sent with `initialize` and `server/discover`,
which clients put in the model's instructions) tell the agent that this vault is its long-term memory, to start each
task with `action=brief` and to end it with `action=finish`: nothing to tell it by hand. Only Claude Code's line has been tried against a real client; the others follow each tool's docs.

**Starting with a memory.** A new cortex can start with what the agent already knows. *Connect an agent* lists the
owner's Claude Code memory folders (`~/.claude/projects/*/memory`, mounted read-only); the ones ticked are imported as
the cortex is made, and `./cortex.sh memories` / `./cortex.sh import <vault> <project>...` do it from the command line,
also into an existing cortex. The import (`B import-memory <folder>... [--commit]`) works by rule, with no model:
- one L1 domain per memory type: Owner profile (user), Working agreements (feedback), Projects, References;
- one L2 concept per memory: the index title as title (a forbidden character becomes a dash, a clash gets the
  project's name), the description as summary, the note under *Idea*, `source: claude-code memory <project>/<file>`;
- `[[links]]` between imported memories become links between their nodes; any other link becomes plain text;
- a memory with what looks like a credential or an address is skipped (allow an address in `allowed_addresses` to
  import it); one over the L2 word limit is captured in `_inbox` for the curator;
- a second import of the same folder adds only what is new; `--commit` makes one commit, as the owner.

**With Python on the host:**

An agent uses a vault once its global instructions say how. `installers/wire_agent.py` writes one block between `<!-- cortex:<slug> -->` markers. Re-running it replaces the block, `--remove` takes it out, and every changed file is first copied to `<file>.bak-<timestamp>`. Use `--dry-run` to see the text first.

```bash
python3 installers/wire_agent.py --agent claude-code --vault ~/vaults/fern-cortex                  # a reader
python3 installers/wire_agent.py --agent claude-code --vault ~/vaults/fern-cortex --role curator   # the curator
python3 installers/wire_agent.py --agent custom --target ~/path/INSTRUCTIONS.md --vault ...
```

| `--agent` | File written | Checked against (2026-10-30) |
|---|---|---|
| `claude-code` | `~/.claude/CLAUDE.md` ("User instructions", all projects) | https://code.claude.com/docs/en/memory |
| `codex` | `$CODEX_HOME/AGENTS.md`, where `CODEX_HOME` defaults to `~/.codex`. An `AGENTS.override.md` there takes precedence | https://learn.chatgpt.com/docs/agent-configuration/agents-md (the redirect target of https://developers.openai.com/codex/guides/agents-md) |
| `gemini` | `~/.gemini/GEMINI.md` | https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md |
| `opencode` | `~/.config/opencode/AGENTS.md`. Without that file, OpenCode falls back to `~/.claude/CLAUDE.md` | https://opencode.ai/docs/rules/ |
| `hermes` | `$HERMES_HOME/SOUL.md` (default `~/.hermes`), plus a skill at `$HERMES_HOME/skills/<slug>/SKILL.md` | https://hermes-agent.nousresearch.com/docs/user-guide/features/personality and https://hermes-agent.nousresearch.com/docs/user-guide/features/skills |

Claude Code skills: personal skills live in `~/.claude/skills/<name>/SKILL.md` and project skills in `.claude/skills/<name>/SKILL.md` (https://code.claude.com/docs/en/skills). `new_vault.py` installs the curator skills at project level, so they load when Claude Code runs inside the vault.

Roles:
- **reader** (the default): recall, read, credit and `learn`. Nothing else.
- **curator**: also runs `consolidate`, links, places the inbox and commits. One curator per vault.

Run the installer yourself. An agent should not rewrite another agent's configuration. Restart the agent afterwards, then test it by asking something only the vault knows, without naming the vault.

**The MCP service (optional, for work that does not need speed).** `scripts/cortex_mcp.py` speaks MCP and runs one vault's own `brain.py` for every call, so an agent that reaches tools by name gets the engine without a shell. Run it once for every agent, as a user service on localhost, or as one stdio process per vault for a client that speaks only stdio.

```bash
python3 scripts/cortex_mcp.py --http 127.0.0.1:8777 --vaults ~/vaults        # every vault, one process
curl -s http://127.0.0.1:8777/health                                          # the vaults it serves
python3 scripts/cortex_mcp.py --vault ~/vaults/fern-cortex --check            # what one vault offers, as json
```

Each agent registers its own vault's path, `http://127.0.0.1:8777/mcp/<vault>?by=<agent>`: `<vault>` is the folder's first word in lower case (`fern` for `fern-cortex`, `claude` for `Claude Cortex`) or its full name, and `?role=reader` hides the curator tool. The service answers MCP's streamable HTTP with plain JSON (no event stream, no server-initiated messages), listens on localhost only and refuses a browser `Origin` that is not local. One gate per vault is shared by every agent, so two agents reading one vault queue instead of racing its session and telemetry.

Two tools. `cortex` is the funnel — `recall`, `read`, `credit`, `stats`, `learn`, `session` — plus combined actions that save round trips: `brief` (at the start of a task: the Hot nodes of `MAP.md`, the budget left and, with a `query`, the `ask` as well), `ask` (recall, then the reads it points at, unless it already handed the answer over), `finish` (credit, then up to five learnings), `health` (stats, session, lint and broken sibling references) and `batch` (up to 12 calls in order, stopping at the first failure). `vault=<sibling>` runs a read on a sibling vault; writes stay on the agent's own. `cortex_curate` carries every other verb of the vault's engine, read from its own parser when the service first serves that vault, so a new engine verb appears with no change to the service; `create` runs `new` and then `link` for each of its links. `status consolidated` is refused unless a stdio server is started with `--allow-consolidate`: only the vault's owner consolidates. `--by <name>` (or `?by=`) sets who authors the learnings; without it the vault's config `learner` does.

| the engine exits | the tool answers |
|---|---|
| 0 | its text, `isError: false` |
| 1 | its `✖` line, `isError: true`: the arguments were wrong, fix them and retry |
| 2 | its limit line, plus what to do with it: stop reading and answer with what you have |

`argparse` exits 2 too and never prints the `✖`, so a call it cannot parse comes back as wrong arguments rather than as a spent budget. A call the engine does not answer in time is stopped and reported the same way.

Run on its own (`cortex_mcp.py --http` or `--vault`), nothing here keeps an engine alive between calls: one costs about 0.04 seconds and 17 MB (timed twice on Alastor, 2026-11-03, engine 2.13.0). The hub's resident engine is different, see *Resident engine* in §8. One call runs at a time per vault, because every read writes the automatic session and the telemetry and the vault lock covers neither. Agents behind the HTTP service share that gate; agents on separate stdio processes do not, and still share the session of §5.

A client that reads Claude Desktop's `mcpServers` block — as Strata's does — takes it verbatim:

```json
{"mcpServers": {"cortex": {"command": "python3",
                          "args": ["/absolute/path/cortex/scripts/cortex_mcp.py", "--vault", "/absolute/path/vault"]}}}
```

Claude Code names the tool `mcp__cortex__cortex`, Strata names it `cortex__cortex`. Restart the client after registering. Test it without one first: the third line must come back with the vault's own counts.

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
              '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
              '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"cortex","arguments":{"action":"stats"}}}' \
  | python3 scripts/cortex_mcp.py --vault ~/vaults/fern-cortex
```

Tests: `python3 -m unittest scripts/test_cortex_mcp.py`.

## 5. The daily loop (any agent)

**Read: the budgeted funnel.**

1. `B recall "<English keywords>"` returns signatures (`[L2] Title — summary (weight 0.42 · seed)`) with context lines (`↑` parent, `↳` typed neighbour). When a small logistic model (the runner-up's score ratio, how much of the query the top's and the runner-up's title, aliases and summary cover, the query length and language) gives the top node at least an 87% chance of being right, recall also prints that body under `# answer:`. Out of fold on the 2026-10-30 bench it fired on 46 of 240 queries and was right on 92%; when it is wrong, the signatures above still list the alternatives.
2. Otherwise, `B read "<exact title>"`, at most two bodies. A long body (over 600 characters) is cut to the lines that match the last query (`# excerpt:`). `read --full` gives the rest once, at no budget cost.
3. Stop when you can answer, after two useless reads, or on **exit code 2**.
4. Cite `[[Title]]`.
5. `B credit --used "<title>" ["<title>" ...]` for the nodes that helped.

Sessions follow the task. `B session new --by <agent>` opens the agent's task session: its `recall`, `read` and `credit` with `--by <agent>` use it with no idle limit until `B session close --by <agent>` or the agent's next `session new` (the MCP's `brief` and `finish` do both). Calls with no task session open use a fallback session, one per agent (`--by`) or one for the vault (no `--by`), renewed after `auto_idle_minutes` (config, default 30) with no call; a spent one is not renewed, so the query limit says to answer. If a session read bodies and credited none, the engine credits the last body it read when the session closes. Any session can also be named with `--session <id>`.

| Mode | Bodies per query | Bodies per session | Queries | Recall output cap |
|---|---|---|---|---|
| quick | 1 | 8 | 8 | 8 lines / 900 chars |
| normal | 5 | 20 | 10 | 20 lines / 900 chars |
| deep | 12 | 40 | 15 | 45 lines / 4,000 chars |

**Hot list.** `MAP.md` → *Hot* lists up to 8 L2/L3 nodes with the highest recent activation. If a summary there answers the question, answer from it without a recall, then `B credit --used "<title>"`.

**Spanish queries** are translated through the engine's `lexicon_es.tsv` and the vault's `_brain/glossary.json`, and recall prints `# searched as: ...`. If a question misses for lack of a word, add the word to the glossary, never a note's title.

**Generated questions (doc2query).** Recall also searches `_brain/questions.jsonl`: for each node, a few English and Spanish questions it answers, written once by an LLM. A Spanish query meets the Spanish questions untranslated. On the 2026-10-30 bench (240 queries, 4 vaults), these questions plus the Snowball stemmer and retuned weights raised hit@1 from 132 to 158.7 out of fold. The engine never calls an LLM itself:

```bash
B questions status                 # covered, stale (the node changed since) and missing nodes
B questions todo --limit 100 > batch.md   # instructions plus the nodes to do; give it to an LLM
B questions import answer.jsonl    # checks ids, drops credential-like text, stores the rest
```

Keep the generator away from any bench file, or the bench stops measuring anything.

**Learn: an agent's own facts.**

```bash
B learn --up "<exact L2 title>" --title "<short English claim>" \
  --summary "<≤140 chars, with the number or name>" --type fact|procedure|error|decision|example \
  --content "<what is true>" --evidence "<file, command, date>" [--truth historical] [--contradicts "<title>"]
```

The engine checks a learning before it writes anything:

| Exit 1 when | Message |
|---|---|
| summary over 140 characters | `summary of N characters (max 140)` |
| an email address, a token, a password-like string, a private key | `possible email address: never store credentials or personal data` |
| Spanish title or summary (ñ ¿ ¡, or two or more Spanish function words) | `title or summary looks Spanish` |
| `[[links]]` in content or evidence | `no links in a learning` |
| content and evidence over 300 words | `one fact per learning` |
| title and summary stems ≥ 60% similar (Jaccard) to an existing node | `"X" already says something close` |
| the title is taken | the reason |

When `--up` names no L2 concept (a new vault has none, or it is an L1), the learning is not refused: once the checks
above pass, it is captured as `_inbox/<date time> <title>.md` (with `learned_by`, `wanted_up`, content and evidence)
for the curator to place with the atomize skill, and the agent is told to create the L1 domain and the L2 concept
first (`cortex_curate verb=create`) to learn straight into the graph next time. Over MCP, `finish` commits it with the
task. It exits with 2 when 25 provisional nodes are already waiting: write the learning to `_inbox/<title>.md` instead.

A new learning is `status: provisional`. It ranks at 0.6× and its weight is ignored. It becomes a `seed` on the `credit` that brings it to 3 distinct sessions of use, provided it is at least 2 days old and has no `contradicts` link in either direction.

## 6. The curator's week

One agent per vault is the curator. It works in a deep session (`B session new --mode deep`) or in a subagent.

1. `B lint` then `B build`. Read *Health* in `_brain/Panel.md` (the `/review` skill).
2. `B consolidate` writes `_brain/proposals.md`:
   - **Hebbian synapses**: node pairs credited together often enough (decayed co-activation ≥ 2.9, about three recent joint uses) with no link yet. Write a real reason and `B link`. With no reason, drop the proposal.
   - **Prune**: synapses with no joint use in 90 days, between low-weight nodes. **Archive**: seeds unused for 60 days with no children, and provisional nodes never used in 30 days. Tick `[x]` and run `B consolidate --apply`.
   - **Atomize** nodes over their word limit (`/atomize`). **Subdomain** for L1s with more than 12 concepts. **Promote / demote** (`B promote`, `B demote --up`).
   - **Merge**: same-layer nodes whose title and summary stems overlap ≥ 75%.
   - **Synthesis**: an L1 whose branches changed after it did. Rewrite `## Synthesis` (≤250 words) from the branch summaries.
   - **Learnings**: pending provisionals, contradictions to decide, and confirmed ones to spot-check.
   - **Validate**: seeds older than 30 days with no human review. List these for the human; the curator never validates.
   - **Inbox**: captures waiting to be atomized.
3. `B lint`, then `git add -A && git commit -m "consolidation: week YYYY-Www"`.
4. Tell the human what changed and what needs a decision, and point them to `git show --stat HEAD`.

**The ideas miner (2.23.0).** `B ideas` is a plain pass over the graph, with no model and no randomness (the same vault
gives the same list), that proposes what the curator may want to build, as notes in `_inbox` and never in the graph:
- **A concept:** a word that recurs in `--min-count` or more L2/L3 titles and summaries (default 4, Settings *Nodes a word
  must recur in*) under at least two different parents, in no domain or concept title or alias; the note lists the nodes
  that carry it and the domain most of them sit under. Create the concept (`new --layer 2`) and `move --up` the nodes that
  belong to it, or delete the note.
- **A link:** two nodes with no link between them (no parent, synapse or `[[mention]]`) that were credited together
  (decayed co-activation from 1.0 up to the point where `consolidate` already proposes it) or share three rare words
  (in at most six nodes). `link` them with a type and a written reason, or delete the note.
At most 5 notes of each kind per run (`--limit`); `--dry-run` writes nothing. Each idea is proposed once: `_log/ideas.jsonl`
remembers every key, so deleting a note dismisses it for good, and a word that gets its concept stops being proposed. The
notes carry `idea: "<key>"` and `learned_by: ideas-miner` and show up under *Inbox* in the next `consolidate`. Run it from
the hub (`./cortex.sh ideas <vault>`, or *Find ideas* in Settings): it commits the notes as the owner
(`ideas: candidates for the curator`), and refuses without `CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`.

**Only the human** runs `B status "<title>" consolidated`. It records `reviewed:` and a `validated` usage event (weight 5).

**Reviewing many seeds at no agent cost.** A local OpenAI-compatible model (Strata on Alastor) prepares the review and the
human decides on the hub's Review page:

1. `python3 scripts/review_prep.py ~/Vaults/"<Vault>" --owner <name>` sends each seed alone, with its parent, siblings,
   truth and usage, and asks for six checks (one idea, specific, fits its parent, English, dated if current, still useful)
   and a verdict: consolidate, fix, obsolete or merge. A consolidate with a failed check becomes fix. Verdicts go to
   `~/Vaults/.cortex-hub/review/<Vault>.jsonl` and progress to `progress.json`. Nothing in the vault changes. It resumes
   where it stopped and judges a node again only when its text changes. `CORTEX_STRATA` sets the model's URL.
2. `https://cortex.local/review` (this machine only) lists the verdicts by domain. Pick consolidate, obsolete, fix later or
   skip per node, or take the model's verdicts for a domain, then **Apply**. `scripts/review_apply.py` changes the statuses
   in one `consolidation:` commit through the hook. Fix and skip are only recorded.

**Verify contracts and system scope (2.13.0).** A `current` L2/L3 note can carry `verify_intent`, `verify_keywords`
and up to five read-only `verify` checks (`key a.b in <file> = v`, `nokey`, `path`, `nopath`, `grep <rx> in <file>`,
`url http://localhost… field = v`, `version <prog> contains t`, `service <unit> active`); `learn` requires them for a
current learning and lint rejects a malformed one. `scripts/renewal.py` runs a note's checks before any model and
routes by intent (history, still_true, drift, policy, needs_experiment). `system: [windows]` scopes a note to one
system: recall prints the host (`CORTEX_SYSTEM`, config `system`, or detected), marks a foreign note `WINDOWS ONLY`
and ranks it lower only when the host has its twin (a host-system note linked to it); renewal leaves it alone.

**REM in the container (2.22.0).** The hub dreams too, so a host with only Docker needs no systemd: turn *REM dreaming* on
in a cortex's Settings (§8), or run `./cortex.sh rem <vault>` (Windows `.\cortex.ps1 rem <vault>`) or *Dream now*. It is the
`cortex-rem` job below, with the differences that matter: it runs from the hub every `rem_interval_minutes` (default
1440), for at most `rem_budget_minutes` (default 30), per cortex; the model server is `CORTEX_STRATA` (default
`http://host.docker.internal:8080`, the host's own; its API key, if it asks for one, goes in
`<vaults>/.cortex-hub/strata.key`); and, because the container has no private store, the answers go through the vault's own
`B questions import` (which checks ids and drops credential-like text) into `_brain/questions.jsonl`, committed as
`rem: questions for N notes` by the owner (`CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`). Without that identity nothing runs, not
even the model call; when something else is staged the questions are written but left uncommitted; when the model server
does not answer the run is skipped and tried at the next interval (it is never started here). Each batch tells the panel
(`brain.py activity dreamed`: the violet twinkles), and the last run's result shows in Settings and in
`<vaults>/.cortex-hub/rem-state.json`. The nightly systemd job stays for hosts that run Python directly; do not run both on
one vault.

**The mechanical phases run on their own** (Linux, systemd user timers): `python3 scripts/lifecycle.py install` sets up
- `cortex-nap`, hourly: mirrors each vault's gitignored `usage.jsonl` into `~/.local/share/cortex-life/usage/` (a fresh clone starts the log empty, so the history lives outside the vault), then rebuilds and lints any vault whose notes or usage changed;
- `cortex-night`, 03:00: `B consolidate`, the vault's bench on a throwaway copy with an alarm when hit@1 or MRR falls, a layer report (fan-out, empty concepts, traffic per domain) in `~/.local/share/cortex-life/reports/`, and a desktop notification when anything needs a look. The only change it makes on its own is pruning: at most 10 synapses per night, only once the usage history spans 90 days (the prune rule reads "no joint use in 90 days") and only with a clean index, committed `consolidation: nightly prune`;
- `cortex-rem`, 01:00: when the local model server answers on `CORTEX_STRATA` (default `http://localhost:8080`; it is never started here), writes the questions each note answers (doc2query), small vaults first and the most used notes first, for at most 120 minutes a night (about 6 s per note). Only new or changed notes are redone. The questions are kept in `~/.local/share/cortex-life/rem/<vault>/questions.jsonl`, ready for `B questions import` once the vault runs engine 2.7.0; nothing is written into a vault.

Every directory under `~/Vaults` with `_brain/scripts/brain.py` is covered (`CORTEX_VAULTS` changes the root). `lifecycle.py status` shows the last runs, the history span, the bench trend and REM progress. Each phase also tells the vault's panel it is running (`brain.py activity`, engine 2.10.0+): a nap as the rings breathing, REM as violet twinkles on the nodes it dreamed, the night bench as an amber sweep.

The curator's week above can run as a weekly scheduled agent: `scripts/deep_sleep_prompt.md` is its prompt. It consolidates each vault on a `consolidation/<week>` branch in its own worktree, benches before and after, and never merges: the human does.

Writing tools for the curator: `new`, `link`/`unlink` (typed: `depends_on`, `contradicts`, `supersedes`, `causes`, `example_of`, `related`; at most 7, each with a reason), `move --up`, `rename` (updates every link, including references in sibling vaults), `promote`, `demote`, and `lint --fix` (fills ids, dates and empty fields). Never move or rename files by hand.

## 7. Sibling vaults and external references

Several agents on one machine each get their own vault. Siblings are declared by name in `config.json`, with paths relative to the vault:

```json
"siblings": {"Kitchen Cortex": "../kitchen-cortex"}
```

- `B recall "<q>" --all` adds a section per sibling, labelled `{Kitchen Cortex}`.
- `B external add "<my node>" --in "Kitchen Cortex" --note "<their node>"` stores an `obsidian://` URI in the node's `external:` property. `read` shows it as `external: Kitchen Cortex::<title>`.
- Read the other vault with its own sessions: `B --vault ../kitchen-cortex read "<title>"`.
- `B externals` lists references and marks broken ones. `B externals --clean` removes broken references and siblings that no longer exist.
- `doctor` fails when a sibling is missing or runs a different engine version, and warns when the version matches but the files differ.

Point at a sibling's node; never copy it. Create siblings before or alongside each other: `doctor` in the first vault fails until the second exists.

## 8. The panel

`B build` (and every commit) regenerates:
- `_brain/Panel.html`: one self-contained page (works offline from disk) with a 2D orbit view and a 3D sphere view of the layers, plus health, growth, top weights, co-activations and recent commits. Open it in a browser. It reads `_brain/state/activity.js`, when present, to show live recall and read activity.
- `_brain/Panel.md`: the same summary for Obsidian, with `generated/orbit.svg`.
- `_brain/generated/index.tsv` (one row per node; the only file agents may grep; the final columns are `id` and `tokens`), `graph.json` and `hot.md`.

**Lightning.** A recall is drawn as a thin storm, not a single line: a stepped leader walks the graph breadth-first from
the query (with a little jitter, so branches start almost together), with a jagged shape, a return stroke that can
re-strike, particles that gather on the nodes that were *read*, and an afterglow. Each recall groups its activated
and read nodes, so the reads show up hierarchically. It needs `activity.js`, which the engine writes on every call.

**Settings button.** When a panel is served by the hub, its header has *Settings*: it opens that cortex's Settings on the
hub (`/agents?settings=<cortex>`). Opened from the disk, or from another machine, the button is not shown.

`Panel.html?embed` shows the orbit alone, filling its frame, and `?theme=dark` or `?theme=light` overrides the saved theme.

**In the container** the hub runs with `--mcp --local-http`: `http://cortex.localhost:8790` and each agent at
`http://<agent>.cortex.localhost:8790`, MCP at `/mcp/<vault>`, HTTPS only on the LAN listener. `compose.trusted.yaml`
brings back the host-trusted setup below (`https://cortex.local`, its DNS and CA).

**Resident engine (2.21.0).** The hub imports the engine once and calls it in its own process. Between calls it keeps,
per vault (at most 8, least recently used dropped): the vault's file list (refreshed every `CORTEX_SCAN_SECONDS`, 5 by
default, and at once after any call that may write notes), the parsed graph, the BM25 index, the parsed glossary and
the decayed usage sums. Every piece carries the stamp it was built from (names, mtimes and sizes of the notes; the
glossary's and questions file's own mtime and size) and is dropped the moment the stamp stops matching, so an edit
made outside (Obsidian) shows up within `CORTEX_SCAN_SECONDS` and one made through the engine at once; session files,
`config.json` and the usage log are still read on every call. `state/telemetry.pickle` is saved at most every 30 s by
a process that keeps the sums in memory; any other process catches up from the log. The MCP token is a separate cache
keyed by `mcp.token`'s mtime and size, so a rotated token works at once. Measure: `docker compose --profile tools run
--rm bench` (add `-v <host folder>:/b` and `--dir /b` to time a bind mount). Engine 2.21.0, 1,000 nodes:

| where | recall before | recall now | read before | read now |
|---|---|---|---|---|
| tmpfs in the container | 27.9 ms | 23.1 ms | 18.9 ms | 14.7 ms |
| Docker Desktop bind mount (Windows, slow: ~10 ms per call on the share) | 842 ms | 96 ms | 757 ms | 61 ms |

A new process per call costs 150 to 175 ms on tmpfs and over a second on that bind mount. What is left on the bind mount
is the writes a recall must make (session, usage log, activity feed) and the stats of the lock and the log.

**Settings of a cortex.** Each card on the hub has *Settings* (this machine only, behind the page's token). Every
change is a click of its own:
- **General and Memory:** name, the agent that learns there (`learner`), domain scope, session idle minutes, weight
  half-life, the sessions and days that confirm a learning, and the provisional cap. Written to `_brain/config.json`
  and committed as the owner (`config: ...`).
- **MCP token:** *New token* makes another one; the old one stops working at once, and the dialog shows the URL and
  header to put in the agent's client.
- **Remote:** the vault's git remote (only its local git config: nothing on the forge changes) and a push token, kept
  in `_brain/state/remote.token`, out of git (migration 0002) and never sent back to a browser; the field works with
  password managers. *Save to remote* is the only way a vault is ever pushed, and only when the owner clicks it: over
  https, with the token in one HTTP header, through the vault's own pre-push hook (a public repository is refused).
- **Delete:** the whole folder, its history and its tokens, for good, only once its exact name is typed. The dialog
  warns first when it has uncommitted changes or was never saved to a remote.

- **Ideas:** the word-recurrence threshold and *Find ideas*, with the number of ideas waiting in the inbox (§6, *The ideas
  miner*). Saved as `ideas_min_count`.
- **REM dreaming:** on or off (off by default), minutes between runs, most minutes per run, and *Dream now*; the dialog shows
  the last run's result and the model server's address (§6, *REM in the container*). Saved in `_brain/config.json` as
  `rem_enabled`, `rem_interval_minutes` and `rem_budget_minutes`.

**Layers** (engine 2.26.0): a vault has three to five. The count is shown, read only, beside the schema; it is the `layers`
key of `_brain/config.json` (migration 0003 wrote `3`; the template says it for new vaults) and it is not a setting. The levels
beyond the third refine a detail: same role (leaf), same types and word limit, and a folder named for the parent
(`Domain/Concept/Detail/Refinement.md`, one folder deeper for L5), so no existing note ever moves when one is added.
`learn`, `import-memory` and the ideas miner keep working on the first three levels; refinements are placed by the curator
(`new --layer 4 --up "<an L3 detail>"`), and `promote` refuses a detail that has refinements.

To add layers: `B layers` shows the count; `B layers --to 4` (or 5) adds them. It is the only way the count changes, only
upward (a smaller number is refused: removing a level means deciding where every note goes, a curator's task), and it writes
the config, the templates `_templates/L4 Refinement.md` and `L5 Refinement.md`, the Layers table and a sentence in the vault's
`AGENTS.md`, and the Obsidian colour groups and `Layers.base` views. Nothing is committed: run `B build`, then commit (from the
host: `./cortex.sh brain <vault> layers --to 4`). The vault must already run this engine (the hub upgrades it on start). `doctor`
fails a count outside 3 to 5. The panel draws a ring, a colour, a filter chip and a legend row per layer (orbit and sphere); a
vault with three layers renders exactly as before. The growth chart still groups every detail level under `L3`.

**The hub: every agent's panel on one page.** `scripts/cortex_hub.py` serves each vault under a root (default `~/Vaults`, or `CORTEX_VAULTS`) with no dependencies:

```bash
python3 scripts/cortex_hub.py --port 8790            # http://127.0.0.1:8790
```

*Agents* shows one card per vault: its orbit, its size by layer, the seeds waiting for review, today's recalls and its latest activity. A card flashes when its agent acts. *Wall* embeds every panel at once (`?embed`), live, in one theme. The views serve only `Panel.html`, `state/activity.js` and `generated/orbit.svg` of each vault; everything else answers 404. Requests naming a host the hub was not given are refused (421), which stops DNS rebinding. As a systemd user service, run from the engine checkout (the hub imports `hub_tls.py` and `installers/wire_agent.py` beside it):

```ini
# ~/.config/systemd/user/cortex-hub.service
[Unit]
Description=Cortex hub (every agent's brain panel)

[Service]
ExecStart=/usr/bin/python3 %h/AI/cortex/scripts/cortex_hub.py --port 8790
Restart=on-failure

[Install]
WantedBy=default.target
```

**Connect an agent.** *Connect an agent* on the hub creates a vault under the root with `scripts/new_vault.py` (or picks an existing one, to share it) and returns a prompt to paste to the agent. The prompt carries the vault's path as the agent's machine writes it, the agent's `wire_agent.py` block (and, for Hermes Agent, its skill), and three steps: run `doctor`, put the block between its markers in the agent's own global instruction file, try one `recall`. The agent wires itself; the hub never touches an agent's files. `custom` agents are told to use the instruction file they load at every start. New vaults get `CORTEX_OWNER` in AGENTS.md and their first commit from `CORTEX_GIT_NAME`/`CORTEX_GIT_EMAIL`; without them the prompt asks the agent to make that commit. The form is a same-origin JSON request carrying the page's token.

**HTTPS and scope.** `--data DIR` turns on HTTPS with the hub's own certificate authority (`scripts/hub_tls.py`, made with `openssl` on first start in `DIR/tls`). The CA carries an X.509 name constraint: it can sign only `cortex.local`, `cortex.home`, `localhost`, their subdomains, loopback and the RFC 1918 ranges, so trusting its `ca.crt` trusts no public site even if its key leaked. The server certificate lasts 397 days and is renewed on start, or at once when the names it needs change; the CA stays. Each cortex has a scope, chosen in *Connect an agent* (and changed there for an existing one), kept in `DIR/scopes.json`:

| Scope | Where | Panel |
|---|---|---|
| Local (default) | this machine only: listener on 127.0.0.1 | `https://claude.cortex.local`; the hub at `https://cortex.local` |
| LAN | the local network, behind the access key in `DIR/access.key` (cookie: HttpOnly, Secure, SameSite=Strict, 30 days) | `https://claude.alastor.cortex.home` (agent, then the machine that hosts it), and `https://10.0.0.100/v/claude/` when the network does not resolve the name |

The machine's name in the LAN host tells several hubs apart and shows whether the network resolves it; the address link always works. The LAN listener (`--lan-address`, `--lan-host`, `--lan-domain cortex.home`) shows only the cortexes marked LAN and never offers the connect form: vaults live on this machine, so only its owner connects agents. Public addresses are refused. `/v/<vault name or agent>/` serves any visible panel by address; `/ca.crt` is also served on the plain-HTTP port, so a phone can fetch the CA before it trusts it.

**DNS.** The hub answers DNS for its own names alone (A records; every other name REFUSED): on `CORTEX_DNS_ADDRESS` (127.0.0.153) for `*.cortex.local` → 127.0.0.1 and `*.<machine>.cortex.home` → the LAN address, and on the LAN address, port 53, for `*.<machine>.cortex.home` only. A more specific route wins, so an existing `~local` route (to a Pi-hole, say) keeps every other `.local` name.

| Where | Route |
|---|---|
| This machine, Linux | `sudo installers/setup_host.sh`: routes both domains to the hub's DNS through a dummy NetworkManager link (`cortex-dns`) and trusts the CA in the system store and the user's Chrome/Chromium store (`--remove` undoes both) |
| This machine, macOS | `/etc/resolver/cortex.local` (and `/etc/resolver/<machine>.cortex.home`) with `nameserver 127.0.0.153`; trust: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.crt` |
| This machine, Windows (admin) | `Add-DnsClientNrptRule -Namespace '.cortex.local','cortex.local' -NameServers 127.0.0.153`; trust: `Import-Certificate -FilePath ca.crt -CertStoreLocation Cert:\LocalMachine\Root` |
| Other devices on the LAN | the router's DNS forwards `<machine>.cortex.home` to the machine (Pi-hole / dnsmasq: `server=/alastor.cortex.home/10.0.0.100`); each device trusts `http://<address>/ca.crt` once |

**In Docker, the same on Linux, macOS and Windows.** At the repository root:

```bash
./install.sh                  # Linux, macOS; Windows (admin PowerShell): install.ps1
```

One command, one password prompt, then nothing else on this machine: it writes `.env` from what it finds (your ids and git identity; this machine's name and private address for the LAN listener, unless `--no-lan`), creates the vault folder, starts the container and runs `installers/setup_host.sh` (DNS route and CA trust). Run it again at any time; it keeps `.env` and the CA. From then on a new agent is the hub's form alone: its host (`<agent>.cortex.local` or `<agent>.<machine>.cortex.home`) resolves and has a valid certificate the moment its cortex exists, and the person only pastes the prompt. The CA's name constraint names the hub's domains and private address ranges, not this machine, so a new address or machine name renews only the server certificate and every machine keeps trusting the CA. `install.ps1` and the macOS branch are written but not yet tried on those systems.

The container mounts `~/Vaults` (`%USERPROFILE%\Vaults` on Windows; `CORTEX_VAULTS` moves it) at `/vaults` and keeps its own data beside the vaults in `.cortex-hub/` (`tls/ca.crt`, `access.key`). It publishes 443 (HTTPS), 80 (redirect only) and the DNS on `CORTEX_ADDRESS` and `CORTEX_DNS_ADDRESS` alone, so no port shows in the address bar; the LAN listener is published only when `COMPOSE_FILE` adds `compose.lan.yaml`, on `CORTEX_LAN_ADDRESS` alone. It runs as `CORTEX_UID:CORTEX_GID` with a read-only root, no capabilities and no new privileges. The engine runs with each agent, on its machine: the agents still need Python 3.10+ and git there. REM runs from the container (§6); the rest of the life cycle (nap, night) stays a host service for now.

Trust the CA by hand, where `setup_host.sh` does not reach (download it from the hub's footer, or take `~/Vaults/.cortex-hub/tls/ca.crt`):

| System | Command |
|---|---|
| Linux, Chrome and Chromium | `certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "Cortex hub" -i ca.crt` |
| Linux, system | `sudo cp ca.crt /usr/local/share/ca-certificates/cortex-hub.crt && sudo update-ca-certificates` |
| Windows (current user) | `Import-Certificate -FilePath ca.crt -CertStoreLocation Cert:\CurrentUser\Root` |
| macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.crt` |
| Firefox | Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import |

The LAN access key: `cat ~/Vaults/.cortex-hub/access.key` on the hub's machine (PowerShell: `Get-Content`). Delete the file and restart to change it; browsers then sign in again.

## 9. Backups

A vault is a git repository with no remote by default. Choose one:
- `git remote add backup <url-or-path> && git push backup main` to a **private** remote (the pre-push hook refuses a public one) or a bare repository on another disk, or
- copy the vault folder. `_brain/state/` caches rebuild themselves; only `usage.jsonl` (the usage log behind weights and Hot) is lost if you skip it.

Restore by cloning or copying, then follow §3 (hooksPath, `chmod +x`, identity) and run `B doctor`. The vault contains no secrets by construction: the hook blocks them.

## 10. Upgrading the engine

Upgrade all siblings together:

```bash
python3 scripts/upgrade_engine.py ~/vaults/fern-cortex ~/vaults/kitchen-cortex [--dry-run]
cd ~/vaults/fern-cortex && git add -A && git commit -m "engine: v2.13.0"
```

The script copies `brain.py`, `brain_core.py`, `snowball.py`, `panel.html`, `lexicon_es.tsv`, `synonyms_en.tsv`, `tests/` and `migrations/` into each vault's `_brain/scripts/` and runs `doctor` in each vault. It does not touch content, config, glossary or templates. Cached state is keyed by engine version, file mtime and size, and Python version, so caches rebuild automatically.

**Migrations: the vault's data catches up by itself.** The engine's code is replaced by the upgrade; the vault's
data (frontmatter, folders, config, `.gitignore`) is brought up to date by **migrations**, deterministic code with no
model involved. Each vault records its `schema` in `_brain/config.json`; a vault from before migrations has none
(schema 0), and a new vault starts on the current one.

```bash
B migrate --dry-run      # what would change, nothing written
B migrate [--commit]     # the pending migrations, in order; --commit makes one commit each
```

- Each pending migration runs under the vault lock, then `lint`: if it raised or left new lint errors, every file it
  touched is restored and the vault stays on the last good schema.
- A migration whose files hold uncommitted changes waits and says which; nothing of yours is overwritten.
- A vault on a newer schema than the engine is left alone (no downgrades). `doctor` reports a vault behind its engine.
- With Docker, the hub runs `migrate --commit` on every vault at start, right after upgrading engines, and commits
  as `Cortex hub` (`migration: 0001 ...`); `./cortex.sh upgrade` does the same by hand.
- Engine state (`_brain/state/`) is never migrated: it is a cache and rebuilds.

**Migrations so far:** 0001 baseline (engine state and the MCP token stay out of git), 0002 the push token stays out of git,
0003 the vault states its number of layers (`layers: 3` in `_brain/config.json`; no note or folder moves; a vault that
already says its count keeps it). Schema 3 is the current one.

**Writing a migration** (engine developers). Add `template-vault/_brain/scripts/migrations/mNNNN_<what>.py` with the
next number. It defines `ID`, `TITLE`, `plan(root)` (the vault-relative paths it would write, `[]` when there is
nothing to do) and `up(root)` (writes only those paths). Rules:
- **Deterministic and idempotent:** the same vault gives the same result, and a second run changes nothing.
- **Frozen:** never read the current template; what schema N means is written in migration N.
- **No guessing:** what cannot be decided by rule goes to `_inbox` with a note for the curator.
- **Tested on fossils:** add a test that builds a vault as it was before, runs `migrate`, checks the result, and
  checks that a second run is a no-op (`MigrationTests` in the engine's tests).

Then bump the engine version and set the template's `config.json` `schema` to the new number.

**Proving a refactor changes nothing (golden output).** `tests/golden_layers.py` runs `lint`, `build` (and the files it
writes), `recall` (a fixed set of queries, normal and deep), `read`-free `stats`, `ideas --dry-run`, `consolidate` and
`doctor` on every vault inside the repository (both example vaults, the empty template vault, a *rich* vault it builds
with every layer, every consolidate rule, lint errors and a usage history, and the 1,000-node performance vault), with
time, dates and ids fixed. `tests/golden_layers.json` holds one SHA-256 per output and `tests/test_golden.py` fails when
any differs (the panel page is compared by its data block; its script is checked in a browser before and after a change).
To look at a difference: `python tests/golden_layers.py capture <dir>` before and after, then `diff -r`. When a change is
*meant* to alter output, regenerate with `python tests/golden_layers.py digest > tests/golden_layers.json`; that diff is
the review. The digest is the same on Windows and in the container.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Commits pass with no lint output, and `MAP.md` never updates | the hook is not executable (common on fresh clones) or `core.hooksPath` is unset | `chmod +x .githooks/pre-commit && git config core.hooksPath .githooks`, then `B doctor` |
| `pre-commit: Python 3.10+ not found` | the hook tries `python`, `py` and `python3` in that order | install Python 3.10+ or put it on PATH |
| `✖ query budget exhausted`, exit 2 | per-query body budget spent | answer, or run a new `recall` if the question changed |
| `✖ session budget exhausted`, exit 2 | per-session body budget spent | answer, or delegate to a subagent in deep mode |
| `✖ limit of N queries in this session`, exit 2 | query limit reached | answer. The next task (`session new`, the MCP's `brief`) starts a new budget; a fallback session renews after `auto_idle_minutes` |
| `✖ "X" was already read in this session`, exit 2 | the body is already in your context | use it. `read --full` only applies after an excerpt |
| `learn` → `summary of 141 characters (max 140)` | summaries are signatures | shorten it |
| `learn` → `possible email address` | an address with a real TLD (anything but `.local`) | replace the person with a role. Keep a deliberate sender in `allowed_addresses` |
| lint `possible secret (password)` blocks a commit | `password:`/`pwd=`/`contraseña:` followed by a value with a digit or symbol, or a token pattern (`sk-`, `ghp_`, `AKIA`, `xox`, private key) | remove it. Store where the secret lives, never its value |
| `learn` → exit 2, `25 provisional nodes await confirmation` | backlog cap | write to `_inbox/`. The curator places it |
| recall finds nothing for an obvious word | the vault never uses that word or any Snowball stem of it | add an alias to the node, a line to `synonyms_en.tsv`, or regenerate the node's questions (`B questions todo`) |
| `could not acquire the vault lock` | another process is writing, or a stale `_brain/state/.lock` | wait. Locks older than 30 seconds are broken automatically |
| `doctor`: `sibling vault "X" not found` | the relative path in `config.json` is wrong, or the sibling does not exist yet | fix the path or create the sibling |
| `doctor`: `"X" runs vX with different files` | engine copies diverged | run `scripts/upgrade_engine.py` on every sibling |
| lint `expected location: ...` | a node's folder does not match its `up` | `B move "<title>" --up "<parent>"`, never by hand |
| lint `not_english` warning | a Spanish title or summary | translate it. Put proper nouns in `aliases` |
| the MCP tool answers `another call to this vault is still running` | the previous call in that server process is still going | retry once: the server keeps one engine call at a time on purpose |
| the MCP client says `ECONNREFUSED` on `cortex.localhost:8790` | the hub container is not running (Docker Desktop was closed or the machine restarted) | start Docker, then `docker compose up -d hub` in the engine folder; the agent's MCP config does not change |
| on Windows, git in a vault shows every file as modified | a Docker bind mount presents host files as mode 0777 | the engine sets `core.fileMode false` when it detects the mount; if it was created elsewhere run `git config core.fileMode false` in the vault |
| scripts fail with `^M` (a carriage return) or `bad interpreter` in the container | CRLF line endings from `core.autocrlf=true` | the repository's `.gitattributes` forces LF (`* text=auto eol=lf`); check the working tree out again |
| `sh ./cortex.sh` does nothing useful in PowerShell | the POSIX script was run from the wrong shell or folder | use `.\cortex.ps1` from the engine folder |
| a read crashed with `auto.json.tmp: No such file or directory` | two processes wrote one session file in the same moment, on an engine copy older than this repository's | `python3 scripts/upgrade_engine.py` on every sibling (§10) |

### Known gaps (not verified or not built)

Each one is parked on purpose, with what to do later and how to know it worked, in [PENDING.md](PENDING.md).

- `install.ps1` and `install.sh` were only syntax-checked; the manual path (`docker compose up -d hub`) is the tested one.
- MCP configs for Codex, Gemini and OpenCode are generated but were not tried against those clients; Claude Code is.
- *Save to remote* was tested only with a stubbed git; try it on a throwaway private repository first.
- Configurable layers (3 to 5, [DESIGN-LAYERS.md](DESIGN-LAYERS.md)) are built but have been tried only on throwaway vaults: add
  a layer to a copy of a real cortex first. Inserting or removing a level stays a curator task, by design.
- REM in the container was tested against a stand-in model server, not a real Strata: try *Dream now* on a small throwaway
  cortex first.

## 12. Reference

Commands, in the order the parser takes them: `session`, `stats`, `recall`, `read`, `credit`, `new`, `learn`, `link`, `unlink`, `move`, `promote`, `demote`, `rename`, `status`, `activity`, `questions`, `lint`, `build`, `precommit`, `prepush`, `consolidate`, `external`, `externals`, `import-memory`, `ideas`, `migrate`, `doctor`. Global flags: `--vault <path>` and `--version`. Over MCP (§4): one tool `cortex`, with `action` among `brief`, `recall`, `read`, `credit`, `stats`, `learn`, `finish`.

Exit codes: 0 ok · 1 usage or validation error · 2 budget or limit spent.

Node frontmatter: `id, layer, type, summary, up, aliases, status, source, external, created, updated, reviewed`, plus optional `truth`, `learned_by`, `confirmed`, `archived`, `tags`, `summary_long`.

Types: L1 `domain`/`tools` · L2 `concept`/`tool` · L3 `fact`/`procedure`/`example`/`error`/`decision`. Statuses: `provisional` (0.6×), `seed` (0.8×), `consolidated` (1.0×), `obsolete` (excluded from recall).
