# Cortex

*By MadLeak · Adhonys Diaz*

**A memory for your AI agents that you can read, check and own.**

AI agents forget everything when a session ends. Cortex gives each of your agents a notebook that survives: what it
learns is written down as short notes, kept on your machine, and shown to you on a live page. You can open the notes,
edit them, and undo any change.

> 🤖 **Are you an AI agent asked to install this?** Follow [docs/AI-RUNBOOK.md](docs/AI-RUNBOOK.md) step by step.

---

## Why use it

- **Your agents stop forgetting.** What an agent learns today is there tomorrow, in any project.
- **You can see everything.** Every memory is a plain text note. Open them in [Obsidian](https://obsidian.md) or any
  editor, and see every change in history.
- **It stays small.** Agents read a few short summaries first and only open full notes when they need to, so memory
  never floods their context.
- **Nothing gets in unchecked.** New facts start as *provisional*. They become trusted only after being useful several
  times, and only you can mark one as final.
- **Secrets and personal data stay out.** Passwords, tokens and email addresses are refused, and a memory can never be
  pushed to a public repository.
- **Light to run.** One Docker container. No database, no cloud account, no API key.

## How it works

```
  Your agent                         Cortex (one Docker container)
  ───────────                        ─────────────────────────────
  starts a task  ── brief ───────▶   "here is what you already know about this"
  does the work
  ends the task  ── finish ──────▶   saves what it learned as short notes
                                     shows it all on a live page for you
```

Each agent has its own memory (a *vault*). Notes are organised in three levels, like a table of contents:

| Level | Example |
|---|---|
| **Domain** | Deployments |
| **Concept** | Deploys happen on Tuesdays |
| **Detail** | Friday deploys were stopped in March |

Works with Claude Code, Codex, Gemini CLI, OpenCode, Hermes, or any app that speaks
[MCP](https://modelcontextprotocol.io).

---

## Get started

You only need **[Docker](https://www.docker.com/products/docker-desktop/)** (Docker Desktop on Windows and macOS).

**1. Download and install**

```bash
git clone https://github.com/MadLeak/cortex-engine.git cortex
cd cortex
./install.sh
```

On Windows, run the last line in PowerShell as:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Prefer plain Docker? `docker compose up -d --build` works on its own: `compose.yaml` already holds every setting. The
installer only adds a `.env` with your name and git identity, so what your agents learn is signed by you.

**2. Open the page**

Go to **http://cortex.localhost:8790** in your browser.

**3. Connect your agent**

Click **Connect an agent**, pick your agent, and copy the line it gives you. For Claude Code it looks like this:

```bash
claude mcp add --transport http --scope user claude-cortex "http://cortex.localhost:8790/mcp/Claude%20Cortex?by=claude-code" --header "Authorization: Bearer <your vault's token>"
```

Restart the agent. That's it: it now checks its memory at the start of each task and saves what it learned at the end.

> Using Claude Code already? Tick your projects in the form and Cortex starts with the memories Claude Code has saved.

---

## Where your notes live (your choice)

You choose whether the notes sit in a folder on your computer or stay inside Docker. One line in the `.env` file
decides it (`CORTEX_VAULTS`); set it before the first start.

| Option | `.env` | Good for |
|---|---|---|
| **A folder on your computer** (default) | nothing to set: `~/Vaults` (Windows `%USERPROFILE%\Vaults`) | Opening the notes in Obsidian or an editor, and backing them up like any files |
| **A folder you pick** | `CORTEX_VAULTS=/path/you/prefer` (Windows `D:\Cortex\Vaults`) | Same, somewhere else (another disk, a synced folder) |
| **No folder, inside Docker only** | `CORTEX_VAULTS=cortex-vaults` | Keeping your computer clean: nothing is mapped, the notes live in a Docker volume |

With *no folder* you still see everything on the hub's page and with `./cortex.sh`, but not in Obsidian, and the
`--trusted` install option is not available. To back that volume up:

```bash
docker run --rm -v cortex_cortex-vaults:/v -v "$PWD":/b alpine tar czf /b/vaults-backup.tgz -C /v .
```

Not sure? Keep the default.

**Changed your mind later?** Your notes come with you. In `.env`, write the new place and the old one, then start
Cortex as usual:

```ini
CORTEX_VAULTS=cortex-vaults                # the new place
CORTEX_VAULTS_FROM=/home/me/Vaults         # the old place (leave it out when the old place was the Docker volume)
```

```bash
docker compose up -d
```

If the new place is empty, every vault is copied there before Cortex starts. The old copy is left untouched: delete it
yourself once you see your agents on the page.

> ⚠️ With the Docker volume, `docker compose down -v` deletes your notes. Use `docker compose down` (without `-v`).

---

## Everyday use

Most of the time you do nothing: the agent uses its memory by itself. When you want to look:

| I want to… | Do this |
|---|---|
| See what my agents know | Open http://cortex.localhost:8790 (one card per agent, live) |
| Read or fix a note | Open the vault folder in Obsidian or any editor |
| List my vaults | `./cortex.sh list` (Windows: `.\cortex.ps1 list`) |
| Check a vault is healthy | `./cortex.sh doctor` |
| Ask a vault something | `./cortex.sh brain <vault> recall "which day do deploys happen"` |
| Get a vault's connection token | `./cortex.sh token <vault>` |
| Start Cortex again after a restart | Open Docker, then `docker compose up -d hub` in the `cortex` folder |
| Update Cortex | `git pull`, then `docker compose up -d --build` in the `cortex` folder (your agent tells you when a new version is out) |

**Staying up to date is automatic.** Every time an agent starts a task, its memory is upgraded to the newest engine
Cortex has, with nothing lost. Once a day Cortex also checks GitHub for a new version; when there is one, your agent
tells you the two commands above. No account, token or image registry is needed: Docker builds Cortex from this folder.

Want to try it without your own data? `./cortex.sh new kitchen-cortex --from kitchen-cortex` creates a small example
vault about a kitchen.

---

## Privacy

The engine is shareable; **your vaults are not**. They hold what your agents learned about you. Cortex refuses to push a
vault to a public repository, and you should never share a vault's notes or pages on the internet.

---

## Learn more

| If you want… | Read |
|---|---|
| Install by an AI agent | [docs/AI-RUNBOOK.md](docs/AI-RUNBOOK.md) |
| Day-to-day operation, every option, troubleshooting | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Why it is designed this way | [docs/PAPER.md](docs/PAPER.md) |
| Decisions and the rules for changing them | [docs/DECISIONS.md](docs/DECISIONS.md) |
| What is not tested yet | [docs/PENDING.md](docs/PENDING.md) |

<details>
<summary><b>Advanced install options</b></summary>

- `./install.sh --lan` (`-Lan`): also serve the vaults you mark *LAN* to your local network, over HTTPS.
- `./install.sh --trusted` (`-Trusted`, administrator): use `https://cortex.local` with no browser warning.
- `./install.sh --remove` (`-Remove`): undo `--trusted`.

See [RUNBOOK §1](docs/RUNBOOK.md#1-install).
</details>

<details>
<summary><b>Without Docker (Python on the host)</b></summary>

Needs Python 3.10+ and git.

```bash
python3 scripts/new_vault.py ~/vaults/my-cortex --name "My Cortex" --agent "my agent" --owner "me"
cd ~/vaults/my-cortex
python3 _brain/scripts/brain.py doctor
python3 installers/wire_agent.py --agent claude-code --vault ~/vaults/my-cortex   # run from the cortex folder
```

See [RUNBOOK §2 to §5](docs/RUNBOOK.md#2-create-a-vault).
</details>

<details>
<summary><b>What is in this repository</b></summary>

| Path | What |
|---|---|
| `install.sh`, `install.ps1` | The installers |
| `cortex.sh`, `cortex.ps1` | The command line (runs in the container) |
| `compose.yaml`, `Dockerfile` | The container; `compose.lan.yaml` and `compose.trusted.yaml` add the LAN and trusted modes |
| `scripts/` | The hub (web page), the MCP service, the command line, vault creation and upgrades |
| `template-vault/` | An empty vault, including the engine (`_brain/scripts/`) and its tests |
| `installers/wire_agent.py` | Connects an agent when you run without Docker |
| `skills/` | Skills for the curator agent: capture, atomize, link, review, consolidate |
| `examples/` | Two made-up example vaults: a garden and a kitchen |
| `docs/` | The guides listed above |
</details>

<details>
<summary><b>Tests</b></summary>

```bash
docker compose --profile tools run --rm test     # every test suite, inside the container
docker compose --profile tools run --rm bench    # how long one memory call takes
```
</details>

## Status

Version **2.26.0**. Cortex comes from one person's working setup of several agents sharing a machine. It works every
day, but it is young: see [what is not tested yet](docs/PENDING.md).

## License

Copyright (c) 2026 Adhonys Diaz. Free for any **noncommercial** use (personal, research, education, charities) under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md). Using it in a product, a paid service or a company needs a
commercial license: open an issue titled "Commercial license".
