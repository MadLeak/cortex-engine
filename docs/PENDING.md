# Pending cases: what to do later, and how to know it worked

Written 2026-11-20 (engine 2.23.0) at the owner's request: these are parked on purpose, not forgotten. Adhonys plans to
move all his cortexes to another place; the cases that need a real environment wait for it. Each entry says why it is
parked, what to do, and what proves it done. When one is done, delete its entry and its line in the RUNBOOK's
*Known gaps*.

## 1. REM against a real model server (Strata)

- **State:** REM dreaming runs in the hub container (2.22.0) and passes its tests, but only against a stand-in model
  server. It was never run against the real local server (`CORTEX_STRATA`, default `http://host.docker.internal:8080`,
  key in `<vaults>/.cortex-hub/strata.key`).
- **Why parked:** the cortexes are moving to another place, and the model server's address and key will change with it.
- **To do later:** on a small throwaway cortex, turn REM on in Settings (or `./cortex.sh rem <vault> --minutes 5`) and
  press *Dream now*. Check that the panel feed shows `dreamed`, that `_brain/questions.jsonl` is committed as the owner
  (`CORTEX_GIT_NAME` / `CORTEX_GIT_EMAIL`), and that nothing else was staged or changed. Then raise the budget.
- **Done when:** one real run produces questions, the commit is the owner's, and a second run on the same notes adds
  nothing. Then remove the stand-in caveat from the RUNBOOK.

## 2. The installers

- **State:** `install.ps1` and `install.sh` were only syntax-checked. The tested path is `docker compose up -d hub`.
- **Why parked:** they should be tried on the machine the cortexes move to, not on this one.
- **To do later:** on the new machine, run the default install (Docker only), then `-Lan` / `--lan`, then `-Remove`;
  `-Trusted` / `--trusted` needs administrator rights and changes the machine's trust store, so try it last, on purpose.
- **Done when:** the hub answers at `http://cortex.localhost:8790` after the default install, and `-Remove` leaves no
  container, image or host change behind (the vault folder stays).

## 3. MCP clients other than Claude Code

- **State:** `mcp_setup` writes configs for Codex, Gemini CLI, OpenCode and a generic `mcpServers` JSON, and a stdio
  bridge. Only Claude Code has connected to a real hub.
- **Why parked:** those programs are not installed on this machine.
- **To do later:** for each client, create a cortex for it from the hub's *Connect an agent*, paste its config, and run
  one `brief` and one `finish` with a throwaway learning. Compare with the client's own documentation for the config path
  and the token header.
- **Done when:** each client's `finish` creates a commit signed by the owner and its panel shows the activity. Fix the
  generated config in `scripts/cortex_hub.py` (`mcp_setup`) where a client differs.

## 4. Save to remote against a real repository

- **State:** *Save to remote* (`hub_settings.push`) was tested with a stubbed git only. It pushes over https, with the
  push token in one HTTP header, through the vault's pre-push hook.
- **Why parked:** the owner does not need it now; vaults are pushed only when asked.
- **To do later:** make a throwaway **private** repository, set it as the cortex's remote and a fine-grained token with
  contents write on that repository only, press *Save to remote*, and confirm the token is not in the output, the URL,
  the vault's git config or any log. Then try a public repository on purpose and confirm the pre-push hook refuses it.
- **Done when:** a private push succeeds, a public one is refused, and the token appears nowhere. Delete the throwaway
  repository and token afterwards. Never use a real vault's remote for this test.

## 5. Number of layers

In progress (decided 2026-11-20): see [DESIGN-LAYERS.md](DESIGN-LAYERS.md). Step 1 (roles instead of the numbers
1, 2, 3, golden-proved) shipped in 2.24.0 and step 2 (`layers: 3` in `_brain/config.json`, migration 0003, schema 3, shown read
only in Settings) in 2.25.0. Left: step 3 (levels 4 and 5, generated panel, an explicit command to add one). Still parked inside it: the 240-query recall bench
lives in the owner's vaults, so hit@1 was not re-measured; run it once on a copy when the vaults move.
