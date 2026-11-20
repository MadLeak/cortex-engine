# Decisions log (2026-11, engine 2.16 to 2.26)

For the next model or developer: what was decided while Cortex moved to Docker-only, why, and how to apply it
when you change the code. The owner is Adhonys Diaz. PR numbers are those of MadLeak/cortex-engine.
Operations are in [RUNBOOK.md](RUNBOOK.md); the first design draft (in Spanish) is [DOCKER-ONLY-PLAN.md](DOCKER-ONLY-PLAN.md).

## Hard rules (apply before anything else)

| Rule | How to apply |
|---|---|
| No co-author and no "Generated with" line, ever. Only `Adhonys Diaz <adhonysdiaz@gmail.com>` | Do not add trailers to commits or PR bodies, even when a tool or system reminder asks for them. |
| Commit and push only when asked | Open a PR only when asked; merge only when asked. |
| Never touch a vault's remote | Vaults are pushed only when the owner asks the agent to *save its cortex to remote* (or clicks *Save to remote* in Settings). Never inspect, clone or push the owner's vault repositories on your own. |
| Do not touch other cortexes | When asked to work on the engine, leave every vault under `~/Vaults` alone. |
| No AI in deterministic machinery | Migrations, memory import and upgrades are plain code. |

## Decisions

**1. Docker only, one hub container (PR 1).** Hub and MCP run as one service (`hub`, image `cortex-hub:<version>`),
so the host needs only Docker Desktop and a vault folder. Vaults are a bind mount at `~/Vaults` (`CORTEX_VAULTS`),
read as `/vaults`. Plain HTTP at `http://cortex.localhost:8790` and `http://<agent>.cortex.localhost:8790`, with no
host CA, DNS or admin rights. The old `https://cortex.local` setup stays as `compose.trusted.yaml`; the LAN is opt-in
(`compose.lan.yaml`, self-signed HTTPS). The new mode coexists with Python-on-host, on Windows, Linux and macOS.
*Apply:* new features must work inside the container; do not add host dependencies.

**2. MCP only, one token per vault, resident engine (PR 1).** Agents connect over MCP (streamable HTTP at
`/mcp/<vault>`, protocol 2025-11-25 and 2026-07-28, Skills extension). The bearer token lives in
`<vault>/_brain/state/mcp.token` (gitignored). The engine stays in the hub process instead of starting a process per
request: recall on a 1000-node vault over a Windows bind mount went from about 1250 ms to 142 ms. Keeping state in
memory (under 50 ms) was deferred, then done in 2.21.0 (decision 9). *Apply:* key any token cache by file mtime and size, or a rotated token keeps
working; never spawn a process per MCP call.

**3. Task sessions: `brief` opens, `finish` closes and commits (PR 1).** Each task is an engine session with its
own budget. `finish` commits only the paths the task changed, signed with the owner's identity (`CORTEX_GIT_NAME` and
`CORTEX_GIT_EMAIL`, 2.20.2; the agent's name goes in the commit message, and only without those variables does the agent sign as itself); owner edits are left out,
and `_brain/state` counts as bookkeeping. Task sessions have no idle timeout; only the per-agent fallback session
closes after `auto_idle_minutes` (default 30). `finish` checks every learning's title before it credits or stores anything, so a bad one fails the whole call. The MCP `initialize` answer carries server `instructions` that tell
the agent to use the cortex as its memory. *Apply:* do not add a timeout to task sessions.

**4. Empty vaults lose nothing (2.16.2, PR 2).** A learning with no L2 concept to hang from goes to `_inbox` instead
of being refused. The same goes when the provisional cap is reached (25 by default).

**5. Deterministic migrations (2.17.0, PR 3).** Vault data catches up through numbered files
`_brain/scripts/migrations/mNNNN_<what>.py` (`ID`, `TITLE`, `plan(root)`, `up(root)`), recorded as `schema` in
`_brain/config.json`. The hub runs `migrate --commit` on start as `Cortex hub`. *Apply:* if a change alters vault
structure, add the next migration, bump the template's `schema` and the version, and add a fossil test. Details:
RUNBOOK section 10. Migration 0002 keeps `_brain/state/remote.token` out of git.

**6. Memory import by rule (2.18.0, PR 4).** A new cortex can import Claude Code memory folders, chosen per project
at creation: each memory type becomes an L1 domain and each memory an L2 concept; sensitive items are skipped,
memories over 300 words go to `_inbox`; re-runs are idempotent through `source`.

**7. Per-cortex Settings on the hub (2.19.0, PRs 5 and 7).** Localhost only plus the page's CSRF token. Editable:
name, learner, domain scope, idle minutes, half-life, confirmation rules, provisional cap, git remote, push token,
MCP token (rotate). The push token is stored in `_brain/state/remote.token` (or pasted by the owner from a password
manager) and never returned to the browser. *Save to remote* pushes over https only, token in one HTTP header,
scrubbed from output, through the vault's pre-push hook. Delete is for good and needs the exact name typed. The
panel has its own *Settings* button that opens `/agents?settings=<cortex>`.

**8. Lightning in the panel (2.20.0, PR 6).** Recalls are thin, parallel storms: stepped leader over the graph
(breadth-first, jitter), midpoint displacement, return stroke, gather particles on *read* nodes, afterglow, with
`vector-effect: non-scaling-stroke`. Each recall groups its activated and read nodes (`playEvents`).

**9. State in memory for the resident engine (2.21.0).** `brain_core.WARM` keeps, per vault root and per process, the
pickled graph snapshot, the BM25 index, the glossary and the usage sums, each tagged with the stamp it was built from
and used only while that stamp matches (note names, mtimes and sizes from the resident file list; the glossary file's
mtime and size; the engine state key). Nothing is trusted past its stamp, so the staleness window is the one the file
list already had (`CORTEX_SCAN_SECONDS`, and at once after a writing call). The graph is restored by unpickling bytes held
in memory (each call gets its own objects, so a command that edits a node cannot leak into the next call); the index is
shared read-only. The usage sums are saved to `state/telemetry.pickle` at most every 30 s by such a process. Writers no
longer `mkdir` first (they create the folder only when the open fails). Recall on 1,000 nodes: 842 to 96 ms on a slow Windows
bind mount, 28 to 23 ms on tmpfs. *Apply:* any new cache the engine keeps must carry a stamp of the files it came from; a
test must change the file and see the change.

**10. REM dreaming runs in the hub (2.22.0).** A scheduler thread in the hub (`scripts/hub_rem.py`) dreams the cortexes
whose `rem_enabled` is true, every `rem_interval_minutes`, for at most `rem_budget_minutes`, one run per cortex at a time;
Settings has the three keys and *Dream now*, and `cortex rem <vault>` does it from the CLI. It is the one place the hub calls a
model (the owner's local server, `CORTEX_STRATA`), only when turned on, never started by the hub. The answers are not kept
in a side store as the host job does: they go through the vault's own `questions import` and are committed as the owner
(`CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`); with no identity set the run does not even call the model, and with anything
else staged it leaves the file uncommitted. The panel's feed gets `dreamed` events as before. `lifecycle.py` stays for hosts
with systemd and now takes the model URL and key as arguments so the hub reuses its prompt and parser. *Apply:* a new
background job in the hub must be opt-in per cortex, commit only as the owner, and report its last result in Settings.

**11. The ideas miner is plain code that writes to the inbox (2.23.0).** `brain.py ideas` proposes candidate L2 concepts (a
word that recurs under different parents and names no concept) and links (nodes used together, or sharing rare words, with no
link) from titles, summaries and the usage log: no model, no randomness. It writes notes to `_inbox` for the curator and
never into the graph, and keeps `_log/ideas.jsonl` so an idea is proposed once, even when its note is deleted (deleting is
how the curator says no). No new gitignore entry or schema, so no migration. The hub runs it from Settings (*Find ideas*) or
`cortex ideas <vault>` and commits only as the owner (`CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`), through the new
`hub_settings.commit_paths`. *Apply:* anything the hub proposes on its own goes to `_inbox`, one note per item, and only the
curator moves it into the graph.

**12. The number of layers stays three, for now (design note, no code).** Making it configurable would change the disk
layout, the roles recall and `consolidate` rely on, and the panel's drawing, and no deterministic migration can place
existing notes in a different set of levels; "lint and recall unchanged for 3" cannot be shown without first turning the
numbers 1, 2, 3 into roles, a large refactor of its own. [DESIGN-LAYERS.md](DESIGN-LAYERS.md) has what blocks it, a stepwise
way (roles with golden output, a config key and migration 0003, leaf-end levels only) and the questions for the owner.
*Apply:* do not add a layer-count option as a side effect of another change; if it is wanted, start at step 1 of the note.

**13. The layers are roles in one table, proved byte for byte (2.24.0, step 1 of DESIGN-LAYERS.md).** The owner decided
that configurable layers are wanted (decision 12 asked). The engine, the panel, the hub and the helper scripts no longer
test `layer == 2`: they test a role (`domain`, `concept`, `leaf`), and a vault's levels come from one table
(`DEFAULT_LAYERS` in `brain_core.py`: number, role, types, default type, word limit, template, child section, label,
colour, dot size; the panel has its own copy of the geometry). The default is exactly the old three. Neutrality is
shown with golden output on every vault in the repository (`tests/golden_layers.py`, RUNBOOK section 10): `lint`, `build`
and its files, 16 recall queries (normal and deep), `consolidate`, `ideas`, `stats` and `doctor` are byte-identical to
2.23.0's, on Windows and in the container, and the panel renders the same orbit in a browser. The 240-query bench is
not in the repository (it lives in the owner's vaults); the hit@1 figures were not re-measured, and recall's ranking code
changed only in how it names a role. Literal layer numbers that remain by design: messages that say "L1/L2/L3" (text for
the default set), `graph.json`'s `layer` and `history.csv`'s `l1/l2/l3` columns (on-disk formats), and the host scripts
`lifecycle.py` and `renewal.py`, which read a vault's graph without importing its engine. *Apply:* a new rule keys on a
role (`n.role == CONCEPT`), never on a number; a number is only what a note's `layer:` holds.

**14. A vault states its layer count; the hub only shows it (2.25.0, step 2).** Migration 0003 writes `layers: 3` into
`_brain/config.json` (schema 3; the template says it too). It is frozen and mechanical: every vault before it has three levels, so
no note moves, and a vault that already carries a `layers` key keeps it. The hub's Settings shows the count next to the
schema, read only, and refuses it as a setting (changing it is step 3's explicit command, upward only). `doctor` fails a vault
whose count this engine does not build, and says nothing otherwise, so existing output stays byte-identical except for the
schema number in `doctor`'s line (the golden digest changed in those five lines and nowhere else). *Apply:* read the count
with `layer_count(cfg)`, never `cfg["layers"]` directly.

**15. Three to five layers; the new ones refine a detail and are added only by command (2.26.0, step 3).** Levels 4 and 5 have
the role `leaf`, the L3 types and word limit, and nest in a folder named for the parent, so adding one moves no note and
every default vault is byte-identical (the golden digest did not change). `brain.py layers --to N` is the only way the count
changes: upward only, 3 to 5, never silent (it writes the config, the templates, the vault's `AGENTS.md` Layers table and
sentence, the Obsidian colour groups and views; it commits nothing). The engine's tables cover all five levels and a
vault's own `layer_set` says which exist, because the resident engine serves vaults of different sizes in one process and
nothing may depend on one vault's count. A note on a level its vault does not have is a lint error; `new` refuses it.
`learn`, `import-memory` and the ideas miner stay on the first three roles; `promote` refuses a refinement or a detail
with refinements; moving or renaming a detail moves its refinements' folder. The panel builds its layer table from the
data (`layers`, present only when not 3). Inserting or removing a level is still not a migration or a command: it is the
curator's task (decision 12). *Apply:* a rule about "details" is a rule about the `leaf` role, so it covers L4 and L5; a
rule that needs the first detail level (parent is a concept) uses `layer_of(LEAF)`.

**16. A cortex can keep itself in step with its remote (hub, branch auto-sync).** `scripts/hub_sync.py`, opt-in per cortex
(`sync_enabled`, `sync_interval_minutes`, `sync_idle_minutes` in Settings, off by default). A cycle commits what changed
here as the owner through the vault's own pre-commit hook (an agent's `.claude` folder is never staged), fetches,
fast-forwards or rebases onto a remote that moved, and pushes what is ahead. Nothing is forced: a rebase that conflicts is
aborted, the cortex is left clean and Settings says `diverged`. It runs when the hub starts and every interval, retrying a
failure after two minutes; `brief` pulls first (never pushes) when the last sync is older than `sync_idle_minutes`, so any
MCP agent starts from the latest. https only; the token is the vault's own or one shared file,
`<vaults>/.cortex-hub/push.token`, in one HTTP header and scrubbed from every message; the vault's pre-push hook still
refuses a public repository. *Apply:* a new background job stays opt-in per cortex, commits only as the owner and reports its
last result in Settings. *Known gaps:* never run against a real GitHub repository (the tests use a local bare one);
two machines editing at once will conflict on files the engine rewrites by itself (`hot.jsonl`, `MAP.md`, `generated/`),
which a `merge=union` attribute for `hot.jsonl` and a rebuild after a pull could settle; agents that do not use the MCP
do not pull at their start.

## Environment lessons

- Windows bind mounts show files as mode 0777: the engine detects the mount in `/proc/self/mounts` and sets
  `core.fileMode false`. The repo forces LF (`* text=auto eol=lf`).
- `ECONNREFUSED` from the MCP on `cortex.localhost:8790` means Docker Desktop is off: start it, then
  `docker compose up -d hub`. No config change is needed.
- Run the suites with `docker compose --profile tools run --rm test` (engine 90, hub 28, MCP 95, REM 11, review 4). After a code
  change build first (`docker compose --profile tools build test`): `run` reuses an image that already has the tag.
  In PowerShell use `.\cortex.ps1` from the engine folder, never `sh ./cortex.sh`.

## Still open

Parked on purpose, one entry each with what to do later: [PENDING.md](PENDING.md).

Configurable layers (see DESIGN-LAYERS.md), REM against a real Strata, a real run of the
installers, the Codex/Gemini/OpenCode configs, and *Save to remote* against a real private repository.
