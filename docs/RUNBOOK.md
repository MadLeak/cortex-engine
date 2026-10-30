# Cortex runbook

This is the operations manual for running Cortex vaults day to day. `B` below means `python3 _brain/scripts/brain.py`, run from the vault root. Use `python` or `py` where that is your interpreter. Every command has `-h`.

Contents: 1 Install · 2 Create a vault · 3 Git and the hook · 4 Wire an agent · 5 The daily loop · 6 The curator's week · 7 Sibling vaults · 8 The panel · 9 Backups · 10 Upgrading the engine · 11 Troubleshooting · 12 Reference

---

## 1. Install

Requirements:
- **Python 3.10 or newer.** `B doctor` fails below 3.10 and the hook refuses to run. The engine uses only the standard library. The tests were run on Python 3.14.4.
- **git.** Each vault is its own repository.
- **Obsidian** (optional), to browse and edit notes as a graph. `.obsidian/` in the template hides the engine folders and colours the layers.

Clone this repository anywhere. It is a toolkit, not a vault: vaults live elsewhere and carry their own copy of the engine in `_brain/scripts/`, so each one works without this repository.

Check the engine before you use it:

```bash
cd template-vault/_brain/scripts && python3 -m unittest discover -s tests
```

Expect `Ran 31 tests ... OK`, plus a timing line for the 1,000-node performance test.

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

Every vault is a git repository whose pre-commit hook (`.githooks/pre-commit`) does two things:
1. **Validates.** It runs `B precommit`, which is `lint`: errors block the commit. Errors include secrets, a missing summary, a summary over 140 characters, a broken synapse, a synapse with no reason, more than 7 synapses, a wrong parent layer, a duplicate title or id, and a cycle.
2. **Regenerates.** It rebuilds and stages `MAP.md`, `_brain/Panel.md`, `_brain/Panel.html`, `_brain/generated/` and `_brain/state/history.csv` (plus `_brain/proposals.md` if present).

Setup on a fresh clone of a vault:

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit          # git does not always keep the executable bit
git config user.name  "Your Name"      # local identity, if you have no global one
git config user.email "you@example.org"
B doctor
```

Commit prefixes used by the skills and the curator: `learning:` (new knowledge), `consolidation:` (curation), `manual:` (human captures), `engine:` (engine upgrades), `config:` (configuration).

Never skip the hook with `--no-verify`. It is the only gate that keeps secrets out of history.

`_brain/state/` holds sessions, the usage log and caches. Most of it is gitignored. `history.csv` and `hot.jsonl` are kept.

## 4. Wire an agent

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

## 5. The daily loop (any agent)

**Read: the budgeted funnel.**

1. `B recall "<English keywords>"` returns signatures (`[L2] Title — summary (weight 0.42 · seed)`) with context lines (`↑` parent, `↳` typed neighbour). When the top node's title, aliases and summary cover at least 70% of the query's idf weight, through at least two query words, and no runner-up covers as much, recall also prints that body under `# answer:`.
2. Otherwise, `B read "<exact title>"`, at most two bodies. A long body (over 600 characters) is cut to the lines that match the last query (`# excerpt:`). `read --full` gives the rest once, at no budget cost.
3. Stop when you can answer, after two useless reads, or on **exit code 2**.
4. Cite `[[Title]]`.
5. `B credit --used "<title>" ["<title>" ...]` for the nodes that helped.

Sessions are automatic. `recall`, `read` and `credit` share one until 10 minutes of idle time. If a session read bodies and credited none, the engine credits the last body it read when the session closes. For an explicit session, run `B session new [--mode quick|normal|deep]` and pass `--session <id>`.

| Mode | Bodies per query | Bodies per session | Queries | Recall output cap |
|---|---|---|---|---|
| quick | 1 | 8 | 8 | 8 lines / 900 chars |
| normal | 5 | 20 | 10 | 20 lines / 900 chars |
| deep | 12 | 40 | 15 | 45 lines / 4,000 chars |

**Hot list.** `MAP.md` → *Hot* lists up to 8 L2/L3 nodes with the highest recent activation. If a summary there answers the question, answer from it without a recall, then `B credit --used "<title>"`.

**Spanish queries** are translated through the engine's `lexicon_es.tsv` and the vault's `_brain/glossary.json`, and recall prints `# searched as: ...`. If a question misses for lack of a word, add the word to the glossary, never a note's title.

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
| the parent is not an L2, or the title is taken | the reason |

It exits with 2 when 25 provisional nodes are already waiting: write the learning to `_inbox/<title>.md` instead.

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

**Only the human** runs `B status "<title>" consolidated`. It records `reviewed:` and a `validated` usage event (weight 5).

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
- `_brain/generated/index.tsv` (one row per node; the only file agents may grep), `graph.json` and `hot.md`.

## 9. Backups

A vault is a git repository with no remote by default. Choose one:
- `git remote add backup <url-or-path> && git push backup main` to a private remote or a bare repository on another disk, or
- copy the vault folder. `_brain/state/` caches rebuild themselves; only `usage.jsonl` (the usage log behind weights and Hot) is lost if you skip it.

Restore by cloning or copying, then follow §3 (hooksPath, `chmod +x`, identity) and run `B doctor`. The vault contains no secrets by construction: the hook blocks them.

## 10. Upgrading the engine

Upgrade all siblings together:

```bash
python3 scripts/upgrade_engine.py ~/vaults/fern-cortex ~/vaults/kitchen-cortex [--dry-run]
cd ~/vaults/fern-cortex && git add -A && git commit -m "engine: v2.5.3"
```

The script copies `brain.py`, `brain_core.py`, `panel.html`, `lexicon_es.tsv`, `synonyms_en.tsv` and `tests/` into each vault's `_brain/scripts/` and runs `doctor` in each vault. It does not touch content, config, glossary or templates. Cached state is keyed by engine version, file mtime and size, and Python version, so caches rebuild automatically.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Commits pass with no lint output, and `MAP.md` never updates | the hook is not executable (common on fresh clones) or `core.hooksPath` is unset | `chmod +x .githooks/pre-commit && git config core.hooksPath .githooks`, then `B doctor` |
| `pre-commit: Python 3.10+ not found` | the hook tries `python`, `py` and `python3` in that order | install Python 3.10+ or put it on PATH |
| `✖ query budget exhausted`, exit 2 | per-query body budget spent | answer, or run a new `recall` if the question changed |
| `✖ session budget exhausted`, exit 2 | per-session body budget spent | answer, or delegate to a subagent in deep mode |
| `✖ limit of N queries in this session`, exit 2 | query limit reached | answer. The automatic session renews after 10 idle minutes |
| `✖ "X" was already read in this session`, exit 2 | the body is already in your context | use it. `read --full` only applies after an excerpt |
| `learn` → `summary of 141 characters (max 140)` | summaries are signatures | shorten it |
| `learn` → `possible email address` | an address with a real TLD (anything but `.local`) | replace the person with a role. Keep a deliberate sender in `allowed_addresses` |
| lint `possible secret (password)` blocks a commit | `password:`/`pwd=`/`contraseña:` followed by a value with a digit or symbol, or a token pattern (`sk-`, `ghp_`, `AKIA`, `xox`, private key) | remove it. Store where the secret lives, never its value |
| `learn` → exit 2, `25 provisional nodes await confirmation` | backlog cap | write to `_inbox/`. The curator places it |
| recall finds nothing for an obvious word | the light stemmer differs between forms ending in `-y` and `-ys` (for example "deploy" and "deploys") | try the other form, add an alias to the node, or add a line to `synonyms_en.tsv` |
| `could not acquire the vault lock` | another process is writing, or a stale `_brain/state/.lock` | wait. Locks older than 30 seconds are broken automatically |
| `doctor`: `sibling vault "X" not found` | the relative path in `config.json` is wrong, or the sibling does not exist yet | fix the path or create the sibling |
| `doctor`: `"X" runs vX with different files` | engine copies diverged | run `scripts/upgrade_engine.py` on every sibling |
| lint `expected location: ...` | a node's folder does not match its `up` | `B move "<title>" --up "<parent>"`, never by hand |
| lint `not_english` warning | a Spanish title or summary | translate it. Put proper nouns in `aliases` |

## 12. Reference

Commands: `session`, `recall`, `read`, `credit`, `learn`, `new`, `link`, `unlink`, `move`, `promote`, `demote`, `rename`, `status`, `lint`, `build`, `precommit`, `consolidate`, `external`, `externals`, `doctor`. Global flags: `--vault <path>` and `--version`.

Exit codes: 0 ok · 1 usage or validation error · 2 budget or limit spent.

Node frontmatter: `id, layer, type, summary, up, aliases, status, source, external, created, updated, reviewed`, plus optional `truth`, `learned_by`, `confirmed`, `archived`, `tags`, `summary_long`.

Types: L1 `domain`/`tools` · L2 `concept`/`tool` · L3 `fact`/`procedure`/`example`/`error`/`decision`. Statuses: `provisional` (0.6×), `seed` (0.8×), `consolidated` (1.0×), `obsolete` (excluded from recall).
