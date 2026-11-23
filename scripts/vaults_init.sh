#!/bin/sh
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Gets the vaults place ready before the hub starts; compose.yaml runs it (service "vaults") on every `up`.
#   /vaults  where the vaults live now: the CORTEX_VAULTS folder, or the cortex-vaults Docker volume
#   /from    where they lived before (CORTEX_VAULTS_FROM; unset, the cortex-vaults volume), read only
# 1. When /vaults holds no vault yet (at most the hub's own .cortex-hub) and /from has something, it copies /from over:
#    changing CORTEX_VAULTS and running `docker compose up` moves the vaults. The old place is left as it was.
# 2. It gives /vaults to CORTEX_UID:CORTEX_GID, the user the hub runs as (a new volume or folder starts out as root's).
set -eu
owner="${CORTEX_UID:-1000}:${CORTEX_GID:-1000}"

has_vaults() { [ -n "$(ls -A "$1" 2>/dev/null | grep -vx '.cortex-hub')" ]; }

if ! has_vaults /vaults && [ -n "$(ls -A /from 2>/dev/null)" ]; then
  echo "vaults: the new place is empty; copying the vaults from the old place (CORTEX_VAULTS_FROM) ..."
  cp -a /from/. /vaults/
  chown -R "$owner" /vaults
  echo "vaults: copied. The old place was left as it was: remove it yourself once the hub shows your cortexes."
fi
chown "$owner" /vaults
echo "vaults: ready"
