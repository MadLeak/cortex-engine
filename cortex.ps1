# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Cortex's command line, run in its container: .\cortex.ps1 list · .\cortex.ps1 brain kitchen recall "chutney"
# Nothing but Docker on this machine. See scripts/cortex_cli.py for the commands.
docker compose --project-directory $PSScriptRoot --profile tools run --rm cli @args
exit $LASTEXITCODE
