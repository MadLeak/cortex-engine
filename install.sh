#!/bin/sh
# Installs the Cortex hub on this machine (Linux or macOS) in one go, with the only password prompt at the end:
#   ./install.sh            # again at any time: it keeps .env and the CA, and refreshes the rest
#   ./install.sh --no-lan   # this machine only, no LAN listener
# 1. writes .env when there is none, from what it finds: your ids, git identity, this machine's name and LAN address
# 2. creates ~/Vaults and starts the container (docker compose up -d --build)
# 3. sudo installers/setup_host.sh: routes cortex.local (and <machine>.cortex.home) to the hub's DNS and trusts its CA
# After that, connecting an agent is the hub's form alone: https://cortex.local. Windows: install.ps1.
set -eu
cd "$(dirname "$0")"
LAN=1; [ "${1:-}" = "--no-lan" ] && LAN=0
need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)"; exit 1; }; }
need docker "the container runtime"
docker compose version >/dev/null 2>&1 || { echo "missing: docker compose v2"; exit 1; }
need python3 "agents run each vault's engine with it"
need git "every vault is a git repository"

if [ ! -f .env ]; then
  name=$(git config --global user.name || true); email=$(git config --global user.email || true)
  host=$(hostname -s 2>/dev/null || hostname); host=$(printf %s "$host" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-\n' '-')
  if [ "$(uname)" = Darwin ]; then
    addr=$(ipconfig getifaddr "$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')" 2>/dev/null || true)
  else
    addr=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
  fi
  case "$addr" in 10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;; *) addr="" ;; esac
  sed -e "s/^CORTEX_UID=.*/CORTEX_UID=$(id -u)/" -e "s/^CORTEX_GID=.*/CORTEX_GID=$(id -g)/" \
      -e "s/^CORTEX_OWNER=.*/CORTEX_OWNER=${name:-the owner}/" \
      -e "s/^CORTEX_GIT_NAME=.*/CORTEX_GIT_NAME=$name/" -e "s/^CORTEX_GIT_EMAIL=.*/CORTEX_GIT_EMAIL=$email/" .env.example > .env
  if [ "$LAN" = 1 ] && [ -n "$addr" ]; then
    printf '\n# LAN listener, found by install.sh\nCORTEX_LAN_ADDRESS=%s\nCORTEX_LAN_HOST=%s\nCOMPOSE_FILE=compose.yaml:compose.lan.yaml\n' "$addr" "$host" >> .env
    echo "LAN: https://<agent>.$host.cortex.home on $addr (only the cortexes you mark LAN)"
  fi
  echo "wrote .env"
fi

vaults=$(sed -n 's/^CORTEX_VAULTS=//p' .env | tail -1); vaults=${vaults:-$HOME/Vaults}
mkdir -p "$vaults"   # before docker would create it as root
docker compose up -d --build
ca="$vaults/.cortex-hub/tls/ca.crt"
i=0; while [ ! -f "$ca" ] && [ $i -lt 60 ]; do sleep 1; i=$((i + 1)); done
[ -f "$ca" ] || { echo "the hub did not start: docker compose logs"; exit 1; }

echo "Trusting the hub on this machine (your password, once):"
sudo "$(pwd)/installers/setup_host.sh"
domain=$(sed -n 's/^CORTEX_DOMAIN=//p' .env | tail -1)
echo "Ready: https://${domain:-cortex.local} - fill in 'Connect an agent' and paste the prompt to your agent."
