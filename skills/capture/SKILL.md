---
name: capture
description: Captures raw text (meeting notes, pastes, loose ideas) into the vault's _inbox without processing it. Use it when the user wants to save something quickly to atomize later.
---

# /capture

1. Create `_inbox/<YYYY-MM-DD HHmm> <short topic>.md` with frontmatter `captured: <date>` and `source: <where it comes from>`, and the text in the body, translated to English (keep proper names, code and quoted UI text verbatim).
2. Replace credentials or personal data with roles or `[omitted]` before saving.
3. Do not create nodes or links: /atomize does that.
4. `git add -A` and `git commit -m "manual: capture <topic>"`.
