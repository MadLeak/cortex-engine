#!/bin/sh
# Copyright (c) 2026 Adhonys Diaz · By MadLeak · PolyForm Noncommercial 1.0.0 (see LICENSE.md)
# Installs Cortex on Linux or macOS with Docker alone, no sudo:
#   ./install.sh              # this machine: http://cortex.localhost:8790
#   ./install.sh --lan        # also the local network, HTTPS by the hub's own CA (each device accepts it once)
#   ./install.sh --trusted    # https://cortex.local with no warning: sudo installers/setup_host.sh routes its DNS and
#                             # trusts the hub's CA on this machine (what this script did before; one password)
#   ./install.sh --remove     # sudo: takes the --trusted DNS route and CA off this machine
#   ./install.sh --move-from=OLD   # copies the vaults from where they were (a folder, or cortex-vaults for the Docker
#                             # volume) to where CORTEX_VAULTS in .env says now; the old copy is left as it was
# It writes .env when there is none (your ids and git identity), creates ~/Vaults and starts the container. After
# that, an agent is connected from the hub's "Connect an agent" form, and ./cortex.sh is the command line.
# Windows: install.ps1.
set -eu
cd "$(dirname "$0")"
LAN=0; TRUSTED=0; MOVE_FROM=
for arg in "$@"; do
  case "$arg" in
    --lan) LAN=1 ;;
    --trusted) TRUSTED=1 ;;
    --remove) exec sudo "$(pwd)/installers/setup_host.sh" --remove ;;
    --move-from=?*) MOVE_FROM=${arg#--move-from=}
                    case "$MOVE_FROM" in "~/"*) MOVE_FROM=$HOME/${MOVE_FROM#"~/"} ;; esac ;;   # the shell leaves ~ after = alone
    *) echo "unknown option: $arg (--lan, --trusted, --remove, --move-from=OLD)"; exit 1 ;;
  esac
done
command -v docker >/dev/null || { echo "missing: docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "missing: docker compose v2"; exit 1; }

if [ ! -f .env ]; then
  name=$(git config --global user.name 2>/dev/null || true); email=$(git config --global user.email 2>/dev/null || true)
  sed -e "s/^CORTEX_UID=.*/CORTEX_UID=$(id -u)/" -e "s/^CORTEX_GID=.*/CORTEX_GID=$(id -g)/" \
      -e "s/^CORTEX_OWNER=.*/CORTEX_OWNER=${name:-the owner}/" \
      -e "s/^CORTEX_GIT_NAME=.*/CORTEX_GIT_NAME=$name/" -e "s/^CORTEX_GIT_EMAIL=.*/CORTEX_GIT_EMAIL=$email/" .env.example > .env
  files=compose.yaml
  if [ "$LAN" = 1 ]; then
    host=$(hostname -s 2>/dev/null || hostname); host=$(printf %s "$host" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-\n' '-')
    if [ "$(uname)" = Darwin ]; then
      addr=$(ipconfig getifaddr "$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')" 2>/dev/null || true)
    else
      addr=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
    fi
    case "$addr" in 10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;; *) echo "--lan: no private IPv4 address found"; rm -f .env; exit 1 ;; esac
    printf '\n# LAN listener, found by install.sh\nCORTEX_LAN_ADDRESS=%s\nCORTEX_LAN_HOST=%s\n' "$addr" "$host" >> .env
    files=$files:compose.lan.yaml
    echo "LAN: https://$addr/v/<agent>/ and https://<agent>.$host.cortex.home (the cortexes you mark LAN)"
  fi
  if [ "$TRUSTED" = 1 ]; then
    printf '\n# host-trusted mode, set up by install.sh --trusted\nCORTEX_DOMAIN=cortex.local\nCORTEX_PORT=443\nCORTEX_HTTP_PORT=80\nCORTEX_DNS_ADDRESS=127.0.0.153\n' >> .env
    files=$files:compose.trusted.yaml
  fi
  [ "$files" = compose.yaml ] || printf 'COMPOSE_FILE=%s\n' "$files" >> .env
  echo "wrote .env"
fi
if [ "$TRUSTED" = 1 ] && ! grep -q 'compose.trusted.yaml' .env; then
  echo "--trusted: this .env has no compose.trusted.yaml in COMPOSE_FILE (see .env.example), or remove .env and run again"
  exit 1
fi

val() { sed -n "s/^$1=//p" .env | tail -1; }
vaults=$(val CORTEX_VAULTS); vaults=${vaults:-$HOME/Vaults}
# CORTEX_VAULTS is a folder (a path) or the Docker volume cortex-vaults (no folder on this machine)
project=${COMPOSE_PROJECT_NAME:-cortex}
uid=$(val CORTEX_UID); gid=$(val CORTEX_GID); owner="${uid:-1000}:${gid:-1000}"
mount_of() {   # what docker run mounts for a vaults place: the folder itself, or the volume's full name
  case "$1" in
    */*) printf %s "$1" ;;
    cortex-vaults) printf '%s_cortex-vaults' "$project" ;;
    *) echo "CORTEX_VAULTS must be a folder path or cortex-vaults, not: $1" >&2; exit 1 ;;
  esac
}
target=$(mount_of "$vaults")
case "$vaults" in
  */*) mkdir -p "$vaults" ;;   # a folder: made before docker would make it as root
  *) [ "$TRUSTED" = 0 ] || { echo "--trusted needs the vaults in a folder (CORTEX_VAULTS is a Docker volume): it installs the CA from there"; exit 1; }
     docker volume inspect "$target" >/dev/null 2>&1 || docker volume create --label "com.docker.compose.project=$project" \
       --label com.docker.compose.volume=cortex-vaults "$target" >/dev/null ;;
esac
docker compose build hub
image=$(docker compose config --images hub)
# the container runs as CORTEX_UID: the vaults place must be its, and a new Docker volume starts out as root's
in_box() { docker run --rm --user 0:0 --entrypoint sh "$@"; }

if [ -n "$MOVE_FROM" ]; then
  source=$(mount_of "$MOVE_FROM")
  [ "$source" != "$target" ] || { echo "--move-from: the vaults are already there ($vaults)"; exit 1; }
  case "$MOVE_FROM" in
    */*) [ -d "$MOVE_FROM" ] || { echo "--move-from: no folder $MOVE_FROM"; exit 1; } ;;
    *) docker volume inspect "$source" >/dev/null 2>&1 || { echo "--move-from: no Docker volume $source"; exit 1; } ;;
  esac
  empty='if [ -n "$(ls -A /vaults)" ]; then echo "--move-from: $1 is not empty; nothing was copied"; exit 3; fi'
  in_box -v "$target:/vaults" "$image" -c "$empty" sh "$vaults"   # checked before the hub is stopped
  docker compose stop hub >/dev/null 2>&1 || true   # nothing writes the old place while it is copied
  echo "copying the vaults from $MOVE_FROM to $vaults ..."
  in_box -v "$source:/from:ro" -v "$target:/vaults" "$image" -c \
    "$empty"'
     cp -a /from/. /vaults/ && chown -R "$2" /vaults' sh "$vaults" "$owner"
  echo "moved. The old copy in $MOVE_FROM was left as it was: remove it yourself once the hub shows your cortexes."
fi
case "$vaults" in */*) ;; *) in_box -v "$target:/vaults" "$image" -c 'chown "$1" /vaults' sh "$owner" ;; esac
docker compose up -d
i=0; until docker compose logs hub 2>/dev/null | grep -q 'MCP at'; do
  i=$((i + 1)); [ $i -lt 60 ] || { echo "the hub did not start: docker compose logs hub"; exit 1; }; sleep 1
done

if [ "$TRUSTED" = 1 ]; then
  ca="$vaults/.cortex-hub/tls/ca.crt"
  i=0; while [ ! -f "$ca" ] && [ $i -lt 30 ]; do sleep 1; i=$((i + 1)); done
  [ -f "$ca" ] || { echo "the hub made no CA: docker compose logs hub"; exit 1; }
  echo "Trusting the hub on this machine (your password, once):"
  sudo "$(pwd)/installers/setup_host.sh"
  url="https://$(val CORTEX_DOMAIN)"
else
  domain=$(val CORTEX_DOMAIN); port=$(val CORTEX_PORT)
  url="http://${domain:-cortex.localhost}:${port:-8790}"
fi
echo "Ready: $url - 'Connect an agent' gives the line your agent's MCP client needs. Command line: ./cortex.sh list"
