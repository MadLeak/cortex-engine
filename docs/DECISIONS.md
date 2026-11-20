# Decisions log (2026-11, engine 2.16 to 2.21)

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

## Environment lessons

- Windows bind mounts show files as mode 0777: the engine detects the mount in `/proc/self/mounts` and sets
  `core.fileMode false`. The repo forces LF (`* text=auto eol=lf`).
- `ECONNREFUSED` from the MCP on `cortex.localhost:8790` means Docker Desktop is off: start it, then
  `docker compose up -d hub`. No config change is needed.
- Run the suites with `docker compose --profile tools run --rm test` (engine 67, hub 25, MCP 95, review 4). After a code
  change build first (`docker compose --profile tools build test`): `run` reuses an image that already has the tag.
  In PowerShell use `.\cortex.ps1` from the engine folder, never `sh ./cortex.sh`.

## Still open

Configurable layers, REM dreaming in the container, the ideas miner, a real run of the
installers, the Codex/Gemini/OpenCode configs, and *Save to remote* against a real private repository.
