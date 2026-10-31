#!/bin/sh
# Makes this Linux machine use its Cortex hub with no port and no certificate warning (NetworkManager +
# systemd-resolved). Run once, and again after the CA changes (a new domain, address or machine name):
#   sudo installers/setup_host.sh            # reads the repository's .env
#   sudo installers/setup_host.sh --remove
# 1. DNS: routes cortex.local (and <machine>.cortex.home, when the LAN listener is on) to the hub's own DNS, through a
#    dummy link `cortex-dns`; every other name resolves as before.
# 2. Trust: adds the hub's CA, which can sign only those names, to the system store and to the Chrome/Chromium store
#    of the user who ran sudo. Firefox keeps its own: import it there by hand.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run it with sudo"; exit 1; }
USR=${SUDO_USER:-root}
UHOME=$(getent passwd "$USR" | cut -d: -f6)
ENV="$(dirname "$0")/../.env"
val() { [ -f "$ENV" ] && sed -n "s/^$1=//p" "$ENV" | tail -1; }
DOMAIN=$(val CORTEX_DOMAIN); DOMAIN=${DOMAIN:-cortex.local}
DNS=$(val CORTEX_DNS_ADDRESS); DNS=${DNS:-127.0.0.153}
VAULTS=$(val CORTEX_VAULTS); VAULTS=${VAULTS:-$UHOME/Vaults}
LANHOST=$(val CORTEX_LAN_HOST); LANDOMAIN=$(val CORTEX_LAN_DOMAIN); LANDOMAIN=${LANDOMAIN:-cortex.home}
CA="$VAULTS/.cortex-hub/tls/ca.crt"
NSS="sql:$UHOME/.pki/nssdb"

if [ "${1:-}" = "--remove" ]; then
  nmcli connection delete cortex-dns >/dev/null 2>&1 && echo "DNS route removed" || true
  rm -f /usr/local/share/ca-certificates/cortex-hub.crt && update-ca-certificates >/dev/null
  command -v certutil >/dev/null && sudo -u "$USR" certutil -d "$NSS" -D -n "Cortex hub" 2>/dev/null || true
  echo "CA removed"; exit 0
fi

ROUTES="~$DOMAIN"; [ -n "$LANHOST" ] && ROUTES="$ROUTES ~$LANHOST.$LANDOMAIN"
command -v nmcli >/dev/null || { echo "NetworkManager not found: give a link DNS=$DNS and Domains=$ROUTES"; exit 1; }
nmcli connection delete cortex-dns >/dev/null 2>&1 || true
# a dummy link that carries only the DNS route: a documentation address (RFC 5737), never a default route
nmcli connection add type dummy ifname cortex0 con-name cortex-dns autoconnect yes \
  ipv4.method manual ipv4.addresses 192.0.2.53/32 ipv4.never-default yes \
  ipv4.dns "$DNS" ipv4.dns-search "$ROUTES" ipv4.ignore-auto-dns yes ipv6.method disabled >/dev/null
nmcli connection up cortex-dns >/dev/null
resolvectl flush-caches 2>/dev/null || true
echo "DNS: $ROUTES -> $DNS"

[ -f "$CA" ] || { echo "no $CA yet: start the hub (docker compose up -d) and run this again"; exit 1; }
cp "$CA" /usr/local/share/ca-certificates/cortex-hub.crt && update-ca-certificates >/dev/null && echo "CA: system store"
command -v certutil >/dev/null || apt-get install -y -qq libnss3-tools >/dev/null
sudo -u "$USR" mkdir -p "$UHOME/.pki/nssdb"
sudo -u "$USR" certutil -d "$NSS" -D -n "Cortex hub" 2>/dev/null || true
sudo -u "$USR" certutil -d "$NSS" -A -t "C,," -n "Cortex hub" -i "$CA" && echo "CA: Chrome/Chromium store of $USR"
echo "Done. Restart the browser, then open https://$DOMAIN"
