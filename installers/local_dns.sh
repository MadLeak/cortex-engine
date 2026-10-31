#!/bin/sh
# Routes the Cortex hub's domain (and every <agent>.<domain>) to the DNS the hub container answers, on Linux with
# NetworkManager and systemd-resolved. Every other name keeps resolving as before. Run once per machine, as root:
#   sudo installers/local_dns.sh [domain] [dns address]      # defaults: cortex.local 127.0.0.153 (the .env values)
#   sudo installers/local_dns.sh --remove
set -eu
CON=cortex-dns
if [ "${1:-}" = "--remove" ]; then
  nmcli connection delete "$CON" 2>/dev/null && echo "removed $CON" || echo "no $CON connection"
  exit 0
fi
DOMAIN=${1:-cortex.local}
DNS=${2:-127.0.0.153}
command -v nmcli >/dev/null || { echo "NetworkManager not found: with systemd-resolved alone, give a link DNS=$DNS and Domains=~$DOMAIN (systemd-networkd)"; exit 1; }
nmcli connection delete "$CON" >/dev/null 2>&1 || true
# a dummy link that carries only the DNS route: a documentation address (RFC 5737), never a default route
nmcli connection add type dummy ifname cortex0 con-name "$CON" autoconnect yes \
  ipv4.method manual ipv4.addresses 192.0.2.53/32 ipv4.never-default yes \
  ipv4.dns "$DNS" ipv4.dns-search "~$DOMAIN" ipv4.ignore-auto-dns yes ipv6.method disabled >/dev/null
nmcli connection up "$CON" >/dev/null
resolvectl flush-caches 2>/dev/null || true
echo "routed $DOMAIN and *.$DOMAIN to $DNS (connection $CON)"
resolvectl query "$DOMAIN" 2>&1 | head -1
