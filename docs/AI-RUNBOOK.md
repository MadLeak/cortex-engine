# Cortex AI runbook: self-install by an agent

**Audience: an AI coding agent** (Claude Code, Codex, Gemini CLI, OpenCode, Hermes or similar) that was given this
repository's URL and asked to install Cortex. Humans want the [README](../README.md) and the [operations runbook](RUNBOOK.md).

Follow the phases in order. Each step has a command, the result to expect, and what to do when it differs. Do not skip
a check because the previous one passed. When a step says **STOP**, tell the human what happened and wait.

---

## 0. The contract

**Goal.** At the end, on this machine:

1. the Cortex hub container runs and answers at `http://cortex.localhost:8790`;
2. one vault exists for *you* (the agent doing the install) under the vaults folder;
3. your MCP client is connected to that vault, and one `brief` → `finish` round trip has worked;
4. the human has a short report (§7).

**What you may do without asking:** install nothing on the host except cloning this repository; run `docker` and
`docker compose`; write `.env` in the clone; create the vaults folder; create your own vault; register the MCP server
in your own client's configuration.

**What you must ask the human first:**

| Decision | Why it is theirs |
|---|---|
| The git identity for vault commits (`CORTEX_GIT_NAME`, `CORTEX_GIT_EMAIL`), when `git config --global` has none | Commits are signed as the owner |
| `--lan` or `--trusted` modes | They expose the hub on the network or change the machine's DNS and trust store (administrator) |
| Importing Claude Code memories into the vault | It copies the owner's notes into a new store |
| Where the vaults live: the default folder `~/Vaults`, another folder, or no folder (a Docker volume) | It is their notes; the default is fine when they do not say |
| Any remote or push token for a vault | A vault holds personal data and must stay private |
| Anything in §8 *Never do* | — |

**What you need to know first** (ask once, in one message, if you cannot tell):

- the name you go by as an agent (`claude-code`, `codex`, `gemini`, `opencode`, `hermes`, or a custom short name);
- whether the human wants the default local-only install (recommended; assume yes if they do not say).

---

## 1. Preflight

Detect the system, then run the checks for it. Use POSIX shell on Linux and macOS, PowerShell on Windows.

| Check | Linux / macOS | Windows (PowerShell) | Expected |
|---|---|---|---|
| Docker | `docker version --format '{{.Server.Version}}'` | same | a version; Docker Desktop must be **running** on Windows and macOS |
| Compose v2 | `docker compose version` | same | `Docker Compose version v2.x` or later |
| git | `git --version` | same | any recent version |
| Port 8790 free | `(ss -ltn 2>/dev/null \|\| netstat -an) \| grep 8790` | `Get-NetTCPConnection -LocalPort 8790 -ErrorAction SilentlyContinue` | nothing listening |
| Git identity | `git config --global user.name; git config --global user.email` | same | both set (else ask, see §0) |

- Docker missing or its daemon not running → **STOP**: ask the human to install or start Docker Desktop / Docker Engine.
  Do not install Docker yourself.
- Port 8790 taken → pick a free port and set `CORTEX_PORT` in `.env` in §3. Every URL below then uses that port.
- Python is **not** required on the host for this path. Do not install it.

---

## 2. Get the code

The repository may be private. Clone it to a stable place outside the vaults folder (it is a toolkit, not a vault):

```bash
# Linux / macOS
git clone https://github.com/MadLeak/cortex-engine.git ~/cortex-engine && cd ~/cortex-engine
```

```powershell
# Windows
git clone https://github.com/MadLeak/cortex-engine.git "$env:USERPROFILE\cortex-engine"; Set-Location "$env:USERPROFILE\cortex-engine"
```

- `Repository not found` or an authentication prompt → the repository is private and this machine has no access.
  If `gh auth status` shows a signed-in account, retry with `gh repo clone MadLeak/cortex-engine <path>`. Otherwise
  **STOP**: ask the human to authenticate git (`gh auth login`). Never ask for or type a token yourself.
- The folder already exists and is a clone of this repository → `git pull --ff-only` instead and continue.
- On Windows, scripts must keep LF endings. `.gitattributes` forces them; if a later step fails with `^M` or
  `bad interpreter`, run `git rm --cached -r . && git reset --hard` in the clone (it only rewrites the checkout).

Record the version you installed: the `VERSION = "..."` line of `template-vault/_brain/scripts/brain_core.py`.

---

## 3. Configure `.env`

The installers write `.env` themselves when it is missing (§4), filling the ids and the git identity from the host. You
only edit it when §1 asked for a different port or the human gave an identity:

| Key | Value |
|---|---|
| `CORTEX_PORT` | `8790`, or the free port from §1 |
| `CORTEX_UID`, `CORTEX_GID` | Linux: the output of `id -u` and `id -g` (the installer fills them). Windows/macOS: leave as is |
| `CORTEX_OWNER` | the human's name, as they want it in `AGENTS.md` |
| `CORTEX_GIT_NAME`, `CORTEX_GIT_EMAIL` | the identity vault commits are signed with. **Empty means `finish` cannot commit**: fill them |
| `CORTEX_VAULTS` | where the vaults live. Commented: the host folder `~/Vaults` (`%USERPROFILE%\Vaults`), mapped into the container. A path: another host folder. `cortex-vaults`: no host folder, a Docker volume (no Obsidian, no `--trusted`). Change it only when the human chose; the default is fine. When it changes on a machine that already has vaults, also set `CORTEX_VAULTS_FROM` to the old place (unset means the volume) and run `docker compose up -d`: the `vaults` service copies them into the empty new place. Leave the old copy for the human to delete, and never run `docker compose down -v` (it deletes the volume) |

Never commit `.env` (it is gitignored) and never print its values back in chat beyond the names.

Optional, avoids Docker creating a directory where a file is expected (the shared push token mount):

```bash
mkdir -p ~/.cortex-secrets && touch ~/.cortex-secrets/push.token      # empty: no push token
```

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.cortex-secrets" | Out-Null; New-Item -ItemType File -Force "$env:USERPROFILE\.cortex-secrets\push.token" | Out-Null
```

---

## 4. Install and start the hub

Default, local-only install (no administrator, no host changes):

```bash
./install.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

It writes `.env` if missing, creates the vaults folder, builds and starts the container, and waits for the hub. It is
safe to run again. The first build downloads the base image and can take a few minutes: run it with a long timeout
or in the background.

**Expected:** a last line `Ready: http://cortex.localhost:8790 ...`.

**Verify yourself** (do not trust the line alone):

```bash
docker compose ps hub                          # State: running
docker compose logs hub | grep 'MCP at'        # the MCP endpoint line
curl -s -o /dev/null -w '%{http_code}\n' http://cortex.localhost:8790/   # 200 (use 127.0.0.1 if the name does not resolve in your shell)
```

The installers are only syntax-checked upstream (see [PENDING.md](PENDING.md) §2). If one fails, fall back to the tested
manual path and continue:

```bash
cp .env.example .env            # only if there is no .env; then edit it as in §3
mkdir -p ~/Vaults
docker compose up -d --build hub
```

| Symptom | Fix |
|---|---|
| `the hub did not start` | `docker compose logs hub --tail 50`, fix what it says, run the installer again |
| bind: address already in use | set another `CORTEX_PORT` in `.env`, `docker compose up -d hub` |
| permission denied on `~/Vaults` (Linux) | `CORTEX_UID`/`CORTEX_GID` in `.env` must match `id -u`/`id -g`; `sudo chown` is the human's call: **STOP** and ask |
| CRLF errors (`^M`) | see §2 |

Run the test suite once; it runs in the container and touches no vault:

```bash
docker compose --profile tools run --rm test     # every suite must end in OK
```

A failing suite → **STOP** and report the failing test names and the version from §2.

---

## 5. Create your vault

One vault per agent, named after the agent. Use the command line in the container (`./cortex.sh`, Windows
`.\cortex.ps1`):

```bash
./cortex.sh new claude-cortex --name "Claude Cortex" --agent claude-code
./cortex.sh doctor claude-cortex        # every line ✔
./cortex.sh list                        # the vault, its engine version and its token file
```

Replace `claude` / `claude-code` with your own agent name from §0. Expect `MCP token in claude-cortex/_brain/state/mcp.token`.

- `... already exists` → the vault was made before: reuse it, do not delete it.
- `new` stops before the first commit for lack of a git identity → fill `CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL` (§3),
  `docker compose up -d hub`, and run `./cortex.sh commit <vault> -m "manual: first commit"`.
- To start with the owner's Claude Code memory (only if the human agreed, §0): `./cortex.sh memories` lists the
  projects, `./cortex.sh import <vault> <project>...` imports them by rule.

The hub's web form *Connect an agent* at `http://cortex.localhost:8790` does the same and prints the exact client line;
use it when the human prefers to click.

---

## 6. Connect your MCP client

The MCP endpoint is:

```
http://cortex.localhost:8790/mcp/<vault folder, URL-encoded>?by=<your agent name>
Authorization: Bearer <the vault's token>
```

Get the token with `./cortex.sh token <vault>`. Pass it straight into your client's configuration; **never echo it in
chat, a commit, a log you show, or a note**.

| Client | How to register |
|---|---|
| Claude Code | `claude mcp add --transport http --scope user claude-cortex "http://cortex.localhost:8790/mcp/claude-cortex?by=claude-code" --header "Authorization: Bearer $(./cortex.sh token claude-cortex)"` |
| Codex | a `[mcp_servers.<name>]` block in `~/.codex/config.toml` with that URL and header |
| Gemini CLI | an `httpUrl` entry with `headers` in `~/.gemini/settings.json` |
| OpenCode | a `remote` MCP entry with that URL and header |
| stdio only | `docker exec -i cortex-hub-1 python3 /opt/cortex/scripts/cortex_mcp.py --vault /vaults/<vault> --by <agent>` |

The *Connect an agent* form prints each of these filled in; prefer copying from it over writing them by hand. Only
Claude Code has been tested against a real hub (see [PENDING.md](PENDING.md) §3): for the others, check the client's
own documentation for the config path and header syntax.

Restart or reload the client so it picks up the server. Then the **acceptance test**, through the MCP tool (the tool is
`cortex`; Claude Code names it `mcp__<server>__cortex`):

1. `action=brief`, `query="what is in this vault"` → returns a `## session` block with a session id and budget.
2. `action=finish`, `used=[]`, `learnings=[{"title": "Cortex was installed on this machine", "summary": "Cortex <version> hub runs in Docker at cortex.localhost:8790; this vault was created on <YYYY-MM-DD>.", "type": "fact", "truth": "historical", "content": "...", "evidence": "AI-RUNBOOK install, <date>"}]`
   → accepted. A new vault has no L2 concept, so the learning lands in `_inbox` for the curator; that is expected.
3. `./cortex.sh brain <vault> stats` (or `git -C ~/Vaults/<vault> log --oneline -3`) → shows the new commit, signed
   with the owner's identity.

If the server is not listed in the client after a restart, or the call returns 401, re-check the URL encoding of the
vault name and the `Authorization` header. `ECONNREFUSED` means the container is down: `docker compose up -d hub`.

From now on: start each task with `action=brief` and end it with `action=finish`. The server's own instructions repeat
this, so nothing has to be written into your instruction files.

---

## 7. Report to the human

End with a short report, nothing secret in it:

- engine version (§2) and where the clone lives;
- hub URL and container state;
- the vault created, its folder, and the result of `doctor`;
- which client was connected and whether the `brief` → `finish` test passed;
- test suite result;
- anything you skipped or that needs them: missing git identity, a client you could not test, an installer that
  failed and the manual path you used.

Point them at the panel: `http://<agent>.cortex.localhost:8790`.

---

## 8. Never do

- Push a vault anywhere, add a remote to it, or turn on sync, unless the human asks. A vault must never be public:
  its pre-push hook refuses public remotes; do not bypass it (`CORTEX_ALLOW_UNVERIFIED_PUSH`) on your own.
- Commit with `--no-verify`, or edit `MAP.md`, `_brain/Panel.*` or `_brain/generated/` by hand (they are generated).
- Store secrets, tokens, passwords or personal data in a note. The engine and the hook refuse most of them; do not try
  to get around that.
- Mark a note `consolidated` (`status consolidated`). Only the human does.
- Run `install.sh --trusted` / `install.ps1 -Trusted` or `--remove` without the human's explicit yes: they change DNS
  and the machine's trust store as administrator.
- Delete or overwrite an existing vault, `.env`, or `~/Vaults/.cortex-hub/`.
- Rewrite another agent's configuration or instruction files.
- Use Cortex commercially or redistribute it outside the [license](../LICENSE.md) (PolyForm Noncommercial 1.0.0).

---

## 9. Uninstall (only when asked)

```bash
docker compose down                 # stops the hub; vaults in ~/Vaults are untouched
docker image rm cortex-hub:<version>
```

Remove the MCP entry from your client. The vaults folder and the clone stay unless the human removes them.

---

## Appendix: one-shot prompt

A human can hand any capable agent this single line:

> Install Cortex from https://github.com/MadLeak/cortex-engine following its `docs/AI-RUNBOOK.md` exactly, connect yourself to your own vault, and report back.
