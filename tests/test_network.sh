#!/usr/bin/env bash
# Checks the firewall ruleset syntax and generates a real WireGuard peer, then confirms both configs parse.
# Needs: nft (nftables) and wg/wg-quick (wireguard-tools).   sudo ./tests/test_network.sh
set -euo pipefail
cd "$(dirname "$0")/.."
pass=0; fail() { echo "FAIL: $*"; exit 1; }; ok() { echo "  ok   $*"; pass=$((pass+1)); }

nft -c -f network/nftables.conf && ok "nftables ruleset passes nft -c"
grep -q 'iifname "lan.30" drop' network/nftables.conf && ok "camera VLAN ends in drop"
! grep -qE 'dnat|redirect' network/nftables.conf && ok "no port forwarding (no dnat/redirect rules)"

tmp=$(mktemp -d); trap 'rm -rf "$tmp" network/wireguard/clients/test-phone.conf' EXIT
wg genkey | tee "$tmp/server.key" | wg pubkey > "$tmp/server.pub"
peer=$(WG_ENDPOINT=vpn.example.com SERVER_PUB="$tmp/server.pub" network/wireguard/new-peer.sh test-phone 7)
client=network/wireguard/clients/test-phone.conf
[[ -f $client ]] && ok "client config written"
[[ $(stat -c %a "$client") == 600 ]] && ok "client config is private (mode 600)"
wg-quick strip "$client" > "$tmp/client.stripped" && ok "client config parses (wg-quick strip)"
grep -q "AllowedIPs = 10.20.0.10/32" "$client" && ok "client routes only the NVR (split tunnel)"
grep -q "Address = 10.99.0.7/32" "$client" && ok "client address 10.99.0.7"

sed "s|<contents of /etc/wireguard/server.key>|$(cat "$tmp/server.key")|" network/wireguard/wg0.conf.example > "$tmp/wg0.conf"
echo "$peer" | grep -v '^# ----' | grep -v '^# Client config' >> "$tmp/wg0.conf"
wg-quick strip "$tmp/wg0.conf" > "$tmp/server.stripped" && ok "server config with new peer parses"
client_pub=$(grep PrivateKey "$client" | awk '{print $3}' | wg pubkey)
grep -q "PublicKey = $client_pub" "$tmp/wg0.conf" && ok "server peer key matches the client's private key"
echo "PASS: $pass network checks"
