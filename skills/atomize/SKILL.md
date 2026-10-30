---
name: atomize
description: Turns a long note, an _inbox capture or a _sources document into atomic L1/L2/L3 nodes placed in the network and linked. Use it to process the inbox or when a node exceeds its size.
---

# /atomize <note or path>

`B` = `python3 _brain/scripts/brain.py` (or `python`/`py` where that is the interpreter). Create a session if you have none: `B session new`.

1. Read the source. From the cold ring: `B read "<Title>" --session <id> --cold "atomize"`.
2. List candidate ideas: one idea = one node. Drop anything that would not change an agent's decision. Write everything in English.
3. For each idea, deduplicate: `B recall "<idea>" --session <id> --mode quick`. If it exists, update that node (Edit, `updated:` today).
4. Place it: the most specific existing L1 (domain) and L2 (concept). Create a new L2 only if it groups ≥2 details. A new L1 only with the user's approval.
5. Create general to specific with `B new` (the L2 before its L3s). Titles = claims; summary ≤140 characters.
6. Fill each body with Edit following the template sections (≤300 words; L1 ≤250).
7. Synapses only if an agent would need to jump from one to the other: `B link "<A>" "<B>" --type <t> --reason "<why>"`.
8. If the source was in `_inbox/`, move it to `_sources/` (or propose deleting it if it adds nothing).
9. `B lint` → fix → `git add -A` and `git commit -m "learning: atomize <source>"`.
