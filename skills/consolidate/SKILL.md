---
name: consolidate
description: Sleep cycle of the second brain — recomputes weights and proposes Hebbian synapses, pruning, archiving, merges, layer promotions or demotions, and rewrites L1 syntheses bottom-up. Use it weekly.
---

# /consolidate

`B` = `python3 _brain/scripts/brain.py` (or `python`/`py` where that is the interpreter). Create a session `B session new --mode deep` (or work inside a subagent).

1. `B consolidate` → read `_brain/proposals.md`.
2. Section by section:
   - **Hebbian synapses**: write a real reason and create them with `B link`; if there is no reason, drop them.
   - **L1 synthesis**: rewrite the domain's `## Synthesis` (≤250 words) from its branches' summaries (signatures from `B recall`, without reading every body). Order `## Branches` by importance. Set `updated:` to today.
   - **Merge**: fold the content into the most used node, redirect synapses and mark the other one for archiving.
   - **Atomize**: apply /atomize to the oversized node.
   - **Promote / demote / subdomain**: `B promote "<T>"` · `B demote "<T>" --up "<Concept>"` · `B move`.
   - **Archive / prune**: tick `[x]` in `proposals.md` and run `B consolidate --apply`.
   - **Validate seeds**: do NOT validate them yourself; list them for the user to decide.
3. `B lint` → `git add -A` and `git commit -m "consolidation: week <YYYY-Www>"`.
4. Summarize for the user what changed and what needs their decision; remind them to review the diff (`git show --stat HEAD`).
