# Network design

## Goals
1. **Cameras can't reach the internet or anything except the NVR.** IP cameras run old firmware and often "phone home" to cloud or P2P services; isolating them removes both the data-leak path and the route for an attacker to pivot from a compromised camera.
2. **Nobody reaches a camera from outside.** No port forwarding. Remote viewers come in through WireGuard and reach only the Frigate UI.
3. **Least privilege per role.** Staff can view; admins can configure; only the NVR pulls video.

## VLANs and addressing

| VLAN | Name | Subnet | Gateway | Contents |
|---|---|---|---|---|
| 10 | Management | 10.10.0.0/24 | 10.10.0.1 | Switches, access points, admin workstation |
| 20 | Servers | 10.20.0.0/24 | 10.20.0.1 | NVR host `10.20.0.10` (Linux, Docker, Frigate) |
| 30 | Cameras | 10.30.0.0/24 | 10.30.0.1 | IP cameras `10.30.0.11`–`.16`, DHCP reservations by MAC |
| 40 | Staff | 10.40.0.0/24 | 10.40.0.1 | Office PCs and phones that view cameras |
| — | WireGuard | 10.99.0.0/24 | 10.99.0.1 | Remote devices, one /32 each |

Switch ports for cameras are **access ports on VLAN 30** (untagged, PoE), so a device plugged into a camera port can only ever land on the camera VLAN. Unused ports are disabled.

## Firewall rules (gateway)

Default deny between all VLANs. In order:

| # | From | To | Ports | Action | Why |
|---|---|---|---|---|---|
| 1 | any | any | established/related | Allow | Replies to allowed connections |
| 2 | NVR `10.20.0.10` | Cameras | TCP 554, 80 | Allow | RTSP video; ONVIF/HTTP for time sync and PTZ |
| 3 | Management | Cameras | TCP 80, 443 | Allow | Camera configuration |
| 4 | Staff, WireGuard | NVR | TCP 8971 | Allow | Frigate's authenticated UI only |
| 5 | Management | NVR | TCP 22, 8971 | Allow | NVR administration |
| 6 | Cameras | Gateway | UDP 67, 123 | Allow | DHCP and NTP (correct timestamps on footage) |
| 7 | Mgmt, Servers, Staff | Internet | any | Allow | Updates, normal use |
| 8 | Cameras | anything else | any | **Drop + log** `CAM-DROP` | No internet, no lateral movement |
| 9 | Internet | Gateway | UDP 51820 | Allow | WireGuard: the only open port |

Implemented for a Linux gateway in [`nftables.conf`](nftables.conf) (syntax-checked with `nft -c` in `tests/test_network.sh`).

Things the rules deliberately don't allow:
- **Port 5000** (Frigate's unauthenticated API) is bound to `127.0.0.1` on the NVR, so it isn't reachable even from the servers VLAN.
- **Cameras get no DNS.** NTP is served by the gateway's IP, so cameras have no reason to resolve names; DNS lookups from cameras show up as `CAM-DROP` and reveal what they try to contact.
- **No UPnP.** Turn it off on the gateway; cameras and NVRs use it to open ports silently.

## Same design on UniFi
The rules map directly onto a UniFi gateway: create VLANs 20/30/40 as separate networks; set the camera network to "isolated" (no internet, no inter-VLAN); add allow rules 2–5 as firewall policies from the specific source to the specific destination and port; use UniFi's built-in WireGuard server for rule 9. Rule order matters the same way: specific allows first, the camera block last.

## Remote access: WireGuard, not port forwarding

| | Port forward RTSP/UI | WireGuard |
|---|---|---|
| Exposed to the internet | Camera or NVR login page, scanned within hours | One UDP port that doesn't answer unauthenticated packets |
| Authentication | Camera/NVR password only | Device key + preshared key, then Frigate login |
| Reach after connecting | Whatever was forwarded | Only `10.20.0.10:8971` (AllowedIPs + firewall rule 4) |
| Revoking a lost phone | Change shared passwords | Delete that one peer |

Clients use a **split tunnel** (`AllowedIPs = 10.20.0.10/32`): only NVR traffic goes through the VPN. `network/wireguard/new-peer.sh` creates a device config and QR code.

## Camera hardening checklist
- [ ] Default admin password changed; separate read-only "viewer" account for the NVR (`FRIGATE_RTSP_USER`)
- [ ] Firmware updated, then automatic cloud/P2P features turned off
- [ ] NTP server set to the gateway; time zone correct
- [ ] Unused services off (UPnP, Telnet, SSH if the camera has it)
- [ ] Main stream H.264 1080p for recording, substream 640×360 at 5 fps for detection; keyframe interval = frame rate (1 per second)
- [ ] DHCP reservation recorded in the camera list (name, MAC, IP, switch port, location)
