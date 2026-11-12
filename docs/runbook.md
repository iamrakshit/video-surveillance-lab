# Troubleshooting runbook

Each section starts from what you see (a health-check status, a user report) and narrows down where the fault is:
**camera → switch/cabling → firewall → Frigate → storage → remote access**. Commands run on the NVR host unless noted.
Interfaces and addresses match [`network/network-design.md`](../network/network-design.md).

Quick reference:

| Tool | Answers |
|---|---|
| `ffprobe` | Can the NVR open the stream, and what is it (codec, resolution, fps)? |
| `tcpdump` | Are packets reaching the camera, and what comes back? |
| Firewall log (`CAM-DROP`, `FWD-DROP`) | Did the gateway block it? |
| `docker logs frigate` | What Frigate itself thinks is wrong |
| `wg show` | Is the remote-access tunnel actually up? |

---

## Camera offline

Health check: `OFFLINE`, "Stream unreachable". The NVR can't open the camera's RTSP stream at all.

**1. Is the camera on the network?**
```bash
ping -c 3 10.30.0.16
ip neigh show 10.30.0.16        # MAC present = camera answered ARP recently; FAILED/INCOMPLETE = nothing there
```
No ARP entry: power or cabling. Check the switch port: link up? PoE drawing power? Port on VLAN 30? A camera that reboots in a loop often means the PoE budget is exceeded or the cable is too long.

**2. Open the stream directly and read the error.**
```bash
ffprobe -v error -rtsp_transport tcp -timeout 5000000 \
  -show_entries stream=codec_name,width,height,avg_frame_rate -of compact \
  "rtsp://$CAM_RTSP_USER:$CAM_RTSP_PASSWORD@10.30.0.16:554/stream2"
```

| ffprobe says | Meaning | Next step |
|---|---|---|
| `Connection timed out` | Nothing answers: camera off, wrong IP, or a firewall drops the packets | Step 3 (tcpdump) and step 4 (firewall log) |
| `Connection refused` | Camera is up but nothing listens on 554 | RTSP disabled or on another port in the camera's settings |
| `401 Unauthorized` | Wrong user or password, or the account lacks stream rights | Check the viewer account on the camera; password changed? |
| `404 Not Found` / `400 Bad Request` | Wrong stream path | Paths differ by vendor (`/stream1`, `/Streaming/Channels/101`, `/cam/realmonitor?...`) |

**3. Watch the conversation with tcpdump** (on the gateway, camera side):
```bash
tcpdump -ni lan.30 host 10.30.0.16 and tcp port 554
```
- **SYN repeated, no reply:** camera isn't receiving it. Off, wrong VLAN, or wrong IP.
- **SYN, then RST:** camera is up, port closed (matches "Connection refused"). Real capture from the lab:
  ```
  IP 127.0.0.1.40844 > 127.0.0.1.8555: Flags [S], seq 3731295285, ...
  IP 127.0.0.1.8555 > 127.0.0.1.40844: Flags [R.], seq 0, ack 3731295286, win 0, length 0
  ```
- **Handshake completes, then `RTSP/1.0 401`:** credentials. Add `-A` to tcpdump to see the RTSP text.
- **Packets leave the NVR (`tcpdump -ni eth0 host 10.30.0.16` on the NVR) but never appear on `lan.30`:** the gateway is dropping them; go to step 4.

**4. Check the firewall log.**
```bash
journalctl -k --since "10 min ago" | grep -E "FWD-DROP|CAM-DROP" | grep 10.30.0.16
```
A `FWD-DROP` from `10.20.0.10` to the camera on port 554 means rule 2 is missing or the NVR's IP changed. Fix the rule, don't open the camera VLAN.

**Escalate** (to whoever owns the site/cabling) when the camera has no link or PoE on its port: that's physical.

---

## Stream OK but Frigate not receiving

Health check: `NOT_RECEIVING`. ffprobe opens the stream, but Frigate shows 0 fps. The camera and network are fine; the problem is between Frigate and the stream.

```bash
docker logs --since 15m frigate 2>&1 | grep -i warehouse_aisle
```
| Frigate log | Cause | Fix |
|---|---|---|
| `No frames received from warehouse_aisle in 20 seconds. Exiting ffmpeg...` | Frigate's ffmpeg connects but gets no video | Path or credentials in `config.yml` differ from what ffprobe used; compare the two URLs exactly |
| `Ffmpeg process crashed unexpectedly for warehouse_aisle.` | ffmpeg exits immediately | Read the ffmpeg lines just above it: 401, 404, or an unsupported codec |
| `Unable to read frames from ffmpeg process.` | Decoder can't keep up or the stream format changed | Check codec/resolution with ffprobe; lower detect resolution or enable hardware decode |
| Camera missing from logs entirely | Not in `config.yml`, or config failed to load | `docker logs frigate | head -50` for a config validation error |

Common real causes: the camera password was changed and `.env` wasn't; a firmware update renamed the stream path; the camera was switched to H.265 and the detect stream can't be decoded fast enough on CPU.

---

## Stream stutters or drops frames

Health check: `DEGRADED` ("Frigate receiving 2.1 of 5 fps, 7 reconnects"). Users report jumpy video or grey smears.

1. **Check what the camera actually sends:**
   ```bash
   ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name,width,height,avg_frame_rate,bit_rate -of compact "rtsp://...:554/stream2"
   ```
   If `avg_frame_rate` is lower than configured, the camera itself is throttling (night mode often halves fps).
2. **Packet loss.** `preset-rtsp-generic` already uses TCP; if a camera only supports UDP, smearing is almost always lost packets.
   On the gateway, capture 60 seconds and count TCP retransmissions:
   ```bash
   timeout 60 tcpdump -ni lan.30 host 10.30.0.13 -w /tmp/cam13.pcap
   tshark -r /tmp/cam13.pcap -Y tcp.analysis.retransmission | wc -l     # or Wireshark: Analyze › Expert Information
   ```
   More than a handful per minute points at the cable, a failing switch port, or a weak wireless bridge.
3. **Keyframe interval.** A keyframe every 4+ seconds makes each reconnect lose seconds of video. Set I-frame interval = frame rate on the camera.
4. **NVR load.** `docker stats frigate` and Frigate's System page: if CPU is pegged, `skipped_fps` rises. Detect on the substream at 5 fps, or add hardware decode.

**Escalate** when retransmissions persist after swapping the patch cable: likely a cabling run or switch fault.

---

## No recordings or gaps in recordings

1. Is recording on for that camera, with a `record` role on an input? (`config.yml`)
2. Retention: segments with no motion are deleted after the `record.motion.days`/`continuous.days` setting. "Gaps" at night with nothing happening are expected with `continuous.days: 0`.
3. Disk:
   ```bash
   df -h /media/frigate ; du -sh /media/frigate/recordings/* | sort -h | tail
   ```
4. Permissions or a disk that remounted read-only: `docker logs frigate | grep -i "read-only\|permission"`; `dmesg | grep -i "ext4\|I/O error"`.

---

## Recordings disk filling up

Health check warning: "Recordings disk 12% free". Frigate deletes the oldest recordings when space runs out, so retention silently gets shorter than configured.

- Estimate need: bitrate (Mbit/s) × 10.8 = GB per camera per day of continuous recording. Six 1080p cameras at 4 Mbit/s with motion-only retention typically use 30–40% of that.
- Options, in order: reduce `continuous.days`, keep motion retention, lower main-stream bitrate on cameras that look at empty areas, add disk.

---

## Frigate API unreachable

Health check: every camera `UNKNOWN`, warning "Frigate API unreachable".

```bash
docker ps --filter name=frigate          # running? restarting?
curl -s http://127.0.0.1:5000/api/version
docker logs --tail 50 frigate
```
A restart loop right after an edit is almost always a config error, printed at the top of the log.

---

## Remote access over WireGuard fails

User: "The app says connected but the camera page won't load", or the tunnel won't connect.

1. **Is there a handshake?** On the gateway:
   ```bash
   wg show wg0
   ```
   `latest handshake: 40 seconds ago` = tunnel up, go to step 3. No handshake or several minutes old = step 2.
2. **Do packets arrive?**
   ```bash
   tcpdump -ni wan0 udp port 51820
   ```
   Nothing arrives: wrong endpoint in the client, ISP CGNAT (gateway has no public IPv4), or an upstream firewall. Packets arrive but no handshake: wrong keys; regenerate the peer with `new-peer.sh`.
3. **Tunnel up, page doesn't load:**
   - Client `AllowedIPs` must include `10.20.0.10/32`.
   - Firewall rule 4 (`wg0 → NVR:8971`): check for `FWD-DROP` from `10.99.0.x`.
   - The URL must be `https://10.20.0.10:8971`.
4. **Loads slowly or stalls on mobile data:** MTU. Set `MTU = 1380` in the client's `[Interface]`.

---

## Too many or missed motion events

- **Too many** (trees, headlights, rain, IR switching at dusk): add a motion mask over the moving background; raise `motion.threshold`; make sure `objects.track` lists only what matters (person, car).
- **Missed people at night:** detection runs on the substream; check it still has enough light/resolution at night. Frigate's Debug view shows motion boxes live.

---

## Escalation note template
```
Camera / site: <name, IP, location>          Status: <OFFLINE / NOT_RECEIVING / DEGRADED>
Since: <time first seen in health check>
Checked: ping/ARP <result> · ffprobe <error> · tcpdump <SYN only / RST / 401> · firewall log <drops or none>
Done so far: <actions with times>
Needed: <site visit for cabling/PoE, camera vendor support, firewall change approval>
```
