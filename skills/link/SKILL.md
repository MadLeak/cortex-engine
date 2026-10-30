---
name: link
description: Proposes and creates typed synapses for a vault node, only the necessary ones and with a written reason (max 7). Use it when a node is isolated or after learning something that connects domains.
---

# /link <Title>

`B` = `python3 _brain/scripts/brain.py` (or `python`/`py` where that is the interpreter).

1. `B read "<Title>" --session <id>` and `B recall "<node summary>" --session <id>`.
2. Candidates: nodes an agent would need to consult *together* with this one. Prefer `depends_on` and `contradicts`; `related` only if no more precise type fits.
3. Write each candidate's reason in one English sentence. If you cannot justify it, drop it.
4. Show the list (type, target, reason) to the user and create the approved ones with `B link`.
5. Respect the cap of 7: if it is full, propose which one to remove (`B unlink`).
6. `git add -A` and `git commit -m "learning: synapses of <Title>"`.
