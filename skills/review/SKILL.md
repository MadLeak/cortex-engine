---
name: review
description: Vault health review — lint, orphans, broken links, oversized nodes, stale seeds, non-English text — with a prioritized list of fixes. Use it periodically or before consolidating.
---

# /review

`B` = `python3 _brain/scripts/brain.py` (or `python`/`py` where that is the interpreter).

1. `B lint` and `B build`; then read the «Health» section of `_brain/Panel.md`.
2. Prioritize: lint errors > secrets > broken links > misplaced > orphans > oversized > `not_english` > stale seeds.
3. Fix the mechanical ones (`B lint --fix`, `B move`, child lines in the parent, translating Spanish text to English). Propose to the user whatever needs judgment.
4. If anything changed: `git add -A` and `git commit -m "consolidation: review <date>"`.
