# A configurable number of layers: the plan and its progress (2026-11)

Status (2026-11-20): **wanted; being built in four steps.** The owner decided it is wanted.
**Step 1 is done (engine 2.24.0)**: roles replace the literal numbers, with golden proof. Steps 2 and 3 follow, one PR each.
The rest of this note is the original analysis (written at 2.20.2, when it was not built), kept because it still says why
the steps are ordered as they are.

Original status: **not built, on purpose.** The runbook asked for it only if it could be done safely, with a numbered migration
(RUNBOOK section 10) and with lint and recall unchanged for the default of 3. It cannot be shown safe in one change; this
note says what blocks it and how it could be done in steps, for the owner to decide whether it is worth doing at all.

## What "three layers" is today

It is not one setting. It is a disk layout, a set of roles and a drawing, all keyed to the numbers 1, 2 and 3:

- **Layout on disk.** `expected_path` nests an L1 as `Domain/Domain.md`, an L2 as `Domain/Concept.md` and an L3 as
  `Domain/Concept/Detail.md`; `validate_parent` allows only L1 under MAP or another L1, L2 under L1, L3 under L2;
  `MAX_DEPTH` bounds the folders. `lint` checks every note against this.
- **Roles, not just levels.** Recall treats L3 as the holder of a fact and L2 as its concept: a concept lifts from its best
  detail's match, and a detail that matches alone at least as well goes before its concept (`score`). Hot lists L2 and L3
  only. `consolidate` has per-layer rules (an L1 over 12 concepts becomes subdomains, a central L2 is promoted, an empty L2
  is demoted, word limits per layer). `learn` writes an L3 under an L2, `import-memory` maps memory types to L1 and L2,
  the ideas miner reads L2 and L3. About 130 lines of `brain_core.py` mention a layer.
- **Types and limits.** `TYPES_BY_LAYER`, `DEFAULT_TYPE`, `MAX_WORDS`, three templates in `_templates/`.
- **Presentation.** `Panel.html` (about 90 lines mention a layer) draws three rings and three nested spheres with the
  colours `--l1` to `--l3`; the hub's cards sum `{1, 2, 3}`; the Obsidian graph groups and `Layers.base` name three layers;
  `AGENTS.md`, the skills and the agents' instructions say "L1 to L3".
- **Evidence.** Recall was tuned on a 240-query bench (hit@1 158.7 out of fold, 2026-10-30) over three-layer vaults, and
  the engine's tests, the performance vault and the example vaults all assume three.

## What blocks a safe change

1. **No rule decides the migration of an existing vault.** The migration rules (frozen, deterministic, no guessing) allow
   a step only when the same vault always gives the same result. Adding a level at the leaf end (an L4 below L3) moves no
   existing note, so it is mechanical. Anything else is not: inserting a level between L2 and L3, or removing one, means
   deciding for every note which level it belongs to, and merging levels loses the distinction. That is a decision for the
   curator, and the rules send it to `_inbox`, not to a migration.
2. **"Recall unchanged for 3" cannot be proved without a refactor first.** The engine's behaviour hangs on roles (domain,
   concept, leaf) written as the numbers 1, 2, 3 in about two hundred places across the engine, the panel, the hub and the
   MCP service. Making the count variable means turning those into roles and only then adding levels. Done in one step it
   mixes a behaviour-neutral refactor with a new feature, and nothing could tell a regression from the feature.
3. **The panel is drawn for three rings.** The orbit and the sphere (radii, colours, the layer filter chips, the legend, the
   lightning that walks the graph) need to be generated from the count; the hub and Obsidian files likewise.
4. **Meaning for the agents.** The words "L1 domain, L2 concept, L3 detail" are in every agent's instructions and in the
   curator skills; a vault with other levels would need its own `AGENTS.md`, and the `learn` contract (one fact under one
   concept) has no obvious equivalent for a fourth level.

## A way to do it in steps, if it is wanted

1. **Roles first (no new behaviour). DONE in 2.24.0 (decision 13).** Replace the literal 1, 2, 3 by three roles (`domain`, `concept`, `leaf`) and by
   the config's `layers` list, with the default exactly today's. Prove it with golden output: `lint`, `build`, `recall`
   and `consolidate` byte-identical on the example vaults, the bench vault and a copy of each real vault, and the 240-query
   bench equal to the point. Ship this alone.
2. **Config and migration 0003.** (next) Write `layers: 3` into `_brain/config.json` for every vault (a no-op for content;
   schema 3), so a vault states what it is. Tested on fossils like the earlier migrations.
3. **Leaf-end levels only.** Allow `layers` of 3 to 5, where levels beyond L3 are further refinements of a detail
   (nested folders named for the parent, the L3 types and limits), so no existing note ever moves. Panel rings, colours,
   hub cards, templates and the layer filter are generated from the count. Inserting or removing a level stays a curator task.
4. **Gate.** Each step must keep the 240-query bench and the engine, hub and MCP suites green for the default.

Each step is its own release and pull request. Step 1 is the large one and carries all of the risk.

## What needs the owner's decision

- Is the feature wanted? Three layers are the shape the paper describes; nothing in the current use needs more.
- If it is, is leaf-end growth (levels 4 and 5) enough, or must a vault be able to insert or remove a level? The second
  cannot be a migration.
- Should step 1 wait for a bench run on the real vaults (they are the owner's, and the engine never reads them on its own)?
