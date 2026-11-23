# Security

Cortex keeps what AI agents learn about their person, so a security problem can expose private data. Thank you for
reporting one carefully.

## How to report

Use GitHub's private reporting: **[Report a vulnerability](https://github.com/MadLeak/cortex-engine/security/advisories/new)**
(the *Security* tab of this repository). Only the maintainer sees it.

Please **do not** open a public issue, and never include real vault contents, tokens or personal data. Describe the
problem, the Cortex version (`VERSION` in `template-vault/_brain/scripts/brain_core.py`) and how to reproduce it with a
made-up vault.

## What counts

For example: a way to read a vault or its tokens without the token, to reach the hub from outside its scope (this
machine or the LAN), to push a vault to a public repository despite the pre-push hook, to get secrets past the lint, or
to run code through a note, the MCP service or the hub page.

## What happens next

You get an answer as soon as the maintainer can look at it. Once a fix is published, the advisory is made public with
credit to you, unless you prefer otherwise. Every hub then hears of the fixed version on its next daily update check.

Only the latest version on `main` gets fixes.
