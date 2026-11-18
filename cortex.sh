#!/bin/sh
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Cortex's command line, run in its container: ./cortex.sh list · ./cortex.sh brain kitchen recall "chutney"
# Nothing but Docker on this machine. See scripts/cortex_cli.py for the commands.
here=$(cd "$(dirname "$0")" && pwd)
exec docker compose --project-directory "$here" --profile tools run --rm cli "$@"
