#!/usr/bin/env bash
# Creates a WireGuard client (phone or laptop) that can reach ONLY the NVR's web UI.
#   ./new-peer.sh <device-name> <last-octet>        e.g. ./new-peer.sh phone-rakshit 2   ->  10.99.0.2
# Env: WG_ENDPOINT (public hostname or IP of the gateway), SERVER_PUB (server public key file, default /etc/wireguard/server.pub)
# Writes clients/<device-name>.conf (keep it private; it contains the device's private key),
# prints the [Peer] block to append to the server's wg0.conf, and shows a QR code for the WireGuard mobile app.
set -euo pipefail
name=${1:?usage: new-peer.sh <device-name> <last-octet>}
octet=${2:?usage: new-peer.sh <device-name> <last-octet>}
[[ $octet =~ ^[0-9]+$ ]] && (( octet >= 2 && octet <= 254 )) || { echo "last octet must be 2-254" >&2; exit 1; }
endpoint=${WG_ENDPOINT:?set WG_ENDPOINT, e.g. vpn.example.com}
server_pub=$(cat "${SERVER_PUB:-/etc/wireguard/server.pub}")
nvr=${NVR_IP:-10.20.0.10}
out_dir="$(dirname "$0")/clients"
umask 077; mkdir -p "$out_dir"

priv=$(wg genkey); pub=$(echo "$priv" | wg pubkey); psk=$(wg genpsk)

cat > "$out_dir/$name.conf" <<CONF
[Interface]
PrivateKey = $priv
Address = 10.99.0.$octet/32

[Peer]
PublicKey = $server_pub
PresharedKey = $psk
Endpoint = $endpoint:51820
# Split tunnel: only traffic to the NVR goes through the VPN; the rest of the phone's traffic does not.
AllowedIPs = $nvr/32
# Keeps the tunnel open through mobile carrier NAT
PersistentKeepalive = 25
CONF

cat <<PEER
# ---- append to /etc/wireguard/wg0.conf on the gateway, then: wg syncconf wg0 <(wg-quick strip wg0)
[Peer]
# $name
PublicKey = $pub
PresharedKey = $psk
AllowedIPs = 10.99.0.$octet/32
PEER
echo "# Client config: $out_dir/$name.conf"
if command -v qrencode >/dev/null && [[ -t 1 ]]; then qrencode -t ansiutf8 < "$out_dir/$name.conf"; fi
