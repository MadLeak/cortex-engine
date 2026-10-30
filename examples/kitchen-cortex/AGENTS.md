# AGENTS.md — Kitchen Cortex

Kitchen Cortex is the graph memory of Basil, kept for Sam. It holds what Basil needs to remember across conversations: its role, its environment and tools, its procedures, the work it maintains and its own lessons.

## Who does what
- **Basil reads.** `recall` before acting on anything it may have learned; `credit` what helped.
- **Basil learns with `B learn`**: one fact per call, as a `provisional` L3 under an existing L2 concept, with content and evidence. The engine refuses it before writing anything if it breaks a rule (Spanish title or summary, secrets or email addresses, links, over 300 words, too close to an existing node, too many already pending). A learning that corrects a node passes `--contradicts "<title>"`. Basil never edits, moves or links existing nodes.
- **Use confirms it.** A provisional node ranks low and its credit does not lift it; once it has helped in 3 sessions, is 2 days old and is in no contradiction, `credit` turns it into a seed.
- **the curator agent curates.** `consolidate` lists pending learnings, conflicts and confirmed ones to sample; the curator places `_inbox/`, decides conflicts, links and commits `learning:`.
- **Sam alone marks a node consolidated.**

## Access
- CLI `B` = `python3 _brain/scripts/brain.py`. Sessions are automatic: `recall`, `read` and `credit` share one until 10 minutes idle (`--session <id>` still works). A session that read bodies and credited none credits the last one it read.
- When one answer is clearly ahead, `recall` prints its body under `# answer:`: answer from it, no `read` needed.
- `read` and `# answer:` hand over only the lines of a long body that match the last question, marked `# excerpt:`; `read "<title>" --full` gives the rest once, at no cost to the budget.
- `MAP.md` lists under **Hot** the nodes needed most lately (ACT-R activation of reads and credits). When a summary there answers the question, answer from it with no recall, then `B credit --used "<title>"` so the use counts.
- Exit code 2 = budget spent: stop reading and answer with what you have.
- Never read the vault with generic file tools; only grep on `_brain/generated/index.tsv`.
- A node of a sibling vault appears as an external reference (`<Sibling>::<title>`): read it with `B --vault "<path to sibling>" read "<title>"`.

## Language
Stored knowledge is English. Query in English; a Spanish query is translated through the engine's lexicon and `_brain/glossary.json`.

## Layers
| Layer | What | Max words |
|---|---|---|
| L0 | AGENTS.md + MAP.md (generated) | — |
| L1 | Domain or subdomain | 250 |
| L2 | Concept: one standing fact or rule | 300 |
| L3 | Detail: lesson (fact, error, decision, procedure) or episode (example) | 300 |
| Cold | `_inbox` `_sources` `_log` `_archive` | — |

## Truth
`truth: current` = verify anything measurable against the live system before stating it · `historical` = a dated record, never present state · `superseded` = wrong now, read only for its lesson.

## Read: budgeted funnel
1. `recall "<English query>"` → signatures, and the body when one answer is clear. 2. Otherwise `read` at most the two bodies you need. 3. Stop when you can answer, after 2 useless reads, or on exit 2. 4. Cite [[Title]]. 5. `credit` what helped.

## Never
Mark a node consolidated (only Sam) · edit generated files (MAP.md, _brain/Panel.*, _brain/generated/, _brain/state/) · store credentials or personal data · skip the hook.
