#!/usr/bin/env python3
"""Camera health check for a Frigate NVR.

For every camera in site.yaml it checks two things independently:
  1. the camera itself: can ffprobe open its RTSP stream?
  2. the NVR: is Frigate actually receiving frames from it? (Frigate /api/stats)

Comparing the two tells you where a fault is: stream down = camera or network; stream up but Frigate
not receiving = Frigate config or credentials. Results go to the console, output/status.json and
output/site_map.html (cameras drawn on the site map, coloured by status).

Exit code: 0 all OK, 1 warnings (degraded, storage low, Frigate unreachable), 2 a camera is offline or not recording.

    python camhealth.py                                   # uses site.yaml next to this file
    python camhealth.py --rtsp-url 'rtsp://127.0.0.1:8554/{name}/sub'   # against the simulator
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNBOOK = "../docs/runbook.md"

OK, DEGRADED, UNKNOWN, NOT_RECEIVING, OFFLINE = "OK", "DEGRADED", "UNKNOWN", "NOT_RECEIVING", "OFFLINE"
SEVERITY = {OK: 0, DEGRADED: 1, UNKNOWN: 1, NOT_RECEIVING: 2, OFFLINE: 2}
COLOURS = {OK: "#2f9e5b", DEGRADED: "#d9a21b", UNKNOWN: "#8a8f98", NOT_RECEIVING: "#e0682b", OFFLINE: "#d13b3b"}

# ffprobe error text -> (short cause, runbook anchor)
PROBE_ERRORS = [
    (r"401|Unauthorized", "authentication failed", "camera-offline"),
    (r"404|Not Found|400 Bad Request", "stream path not found", "camera-offline"),
    (r"Connection refused", "connection refused (RTSP service off or wrong port)", "camera-offline"),
    (r"timed out|Timeout|No route to host|Network is unreachable", "no response (camera off, wrong IP, or firewall)", "camera-offline"),
]


@dataclass
class Probe:
    ok: bool
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    error: str = ""


@dataclass
class Result:
    name: str
    label: str
    ip: str
    status: str
    reason: str
    runbook: str = ""
    stream: dict = field(default_factory=dict)
    frigate: dict = field(default_factory=dict)


def expand(template: str, **values: str) -> str:
    """Fill ${ENV_VAR} from the environment and {key} from values."""
    out = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), template)
    return out.format(**values)


def mask_credentials(url: str) -> str:
    return re.sub(r"(rtsp://[^:/@]+:)[^@]*@", r"\1***@", url)


def probe_rtsp(url: str, timeout_s: int = 5) -> Probe:
    """Open the stream with ffprobe (RTSP over TCP) and read the first video stream's properties."""
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(timeout_s * 1_000_000),
           "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
           "-of", "json", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 10)
    except subprocess.TimeoutExpired:
        return Probe(ok=False, error="ffprobe timed out")
    except FileNotFoundError:
        return Probe(ok=False, error="ffprobe not installed")
    streams = json.loads(proc.stdout or "{}").get("streams", []) if proc.returncode == 0 else []
    if not streams:
        return Probe(ok=False, error=mask_credentials((proc.stderr or "no video stream").strip().splitlines()[-1]))
    s = streams[0]
    num, _, den = s.get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    return Probe(ok=True, codec=s.get("codec_name", ""), width=s.get("width", 0), height=s.get("height", 0), fps=round(fps, 2))


def fetch_frigate_stats(api: str, timeout_s: int = 5) -> dict | None:
    try:
        with urllib.request.urlopen(f"{api.rstrip('/')}/stats", timeout=timeout_s) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def explain_probe_error(error: str) -> tuple[str, str]:
    for pattern, cause, anchor in PROBE_ERRORS:
        if re.search(pattern, error):
            return cause, anchor
    return error or "stream unreachable", "camera-offline"


def classify(cam: dict, probe: Probe, stats: dict | None, degraded_ratio: float) -> Result:
    base = dict(name=cam["name"], label=cam.get("label", cam["name"]), ip=cam.get("ip", ""), stream=asdict(probe))

    if not probe.ok:
        cause, anchor = explain_probe_error(probe.error)
        return Result(**base, status=OFFLINE, reason=f"Stream unreachable: {cause}", runbook=anchor)

    if stats is None:
        return Result(**base, status=UNKNOWN, reason="Stream OK; Frigate API unreachable, recording not verified",
                      runbook="frigate-api-unreachable")

    cs = stats.get("cameras", {}).get(cam["name"])
    if cs is None:
        return Result(**base, status=NOT_RECEIVING, reason="Stream OK but camera is not in Frigate's config",
                      runbook="stream-ok-but-frigate-not-receiving")
    fps = float(cs.get("camera_fps", 0))
    expected = float(cs.get("expected_fps") or cam.get("detect_fps", 5))
    quality = cs.get("connection_quality", "")
    frig = {"camera_fps": fps, "expected_fps": expected, "skipped_fps": cs.get("skipped_fps"),
            "connection_quality": quality, "reconnects_last_hour": cs.get("reconnects_last_hour")}

    if fps < 0.1:
        return Result(**base, frigate=frig, status=NOT_RECEIVING,
                      reason="Stream OK but Frigate is receiving no frames", runbook="stream-ok-but-frigate-not-receiving")
    if fps < degraded_ratio * expected or quality in ("poor", "unusable"):
        detail = f"{fps:g} of {expected:g} fps"
        if cs.get("reconnects_last_hour"):
            detail += f", {cs['reconnects_last_hour']} reconnects in the last hour"
        return Result(**base, frigate=frig, status=DEGRADED, reason=f"Frigate receiving {detail}",
                      runbook="stream-stutters-or-drops-frames")
    return Result(**base, frigate=frig, status=OK, reason=f"{probe.codec} {probe.width}x{probe.height}, Frigate {fps:g} fps")


def storage_warning(stats: dict | None, warn_percent: float) -> str | None:
    if not stats:
        return None
    rec = stats.get("service", {}).get("storage", {}).get("/media/frigate/recordings", {})
    if rec.get("total"):
        free_pct = 100 * rec["free"] / rec["total"]
        if free_pct < warn_percent:
            return f"Recordings disk {free_pct:.0f}% free ({rec['free'] / 1024:.0f} GiB of {rec['total'] / 1024:.0f} GiB)"
    return None


# ---------- site map ----------

def _fov_path(cam: dict) -> str:
    x, y, h, fov, r = cam["x"], cam["y"], cam.get("heading", 0), cam.get("fov", 90), cam.get("range", 120)
    def point(angle):  # compass degrees -> SVG coordinates (y grows downward)
        a = math.radians(angle)
        return x + r * math.sin(a), y - r * math.cos(a)
    (x1, y1), (x2, y2) = point(h - fov / 2), point(h + fov / 2)
    return f"M{x},{y} L{x1:.1f},{y1:.1f} A{r},{r} 0 0 1 {x2:.1f},{y2:.1f} Z"


def _label(cam: dict, width: int) -> str:
    near_right = cam["x"] > width - 130          # keep labels inside the map: flip to the left near the edge
    x = cam["x"] - 13 if near_right else cam["x"] + 13
    anchor = "end" if near_right else "start"
    text = html.escape(cam.get("label", cam["name"]))
    return f'<text x="{x}" y="{cam["y"] - 10}" text-anchor="{anchor}" class="cam-label">{text}</text>'


LABELS = {OK: "OK", DEGRADED: "Degraded", UNKNOWN: "Unknown", NOT_RECEIVING: "Not receiving", OFFLINE: "Offline"}


def render_map(site: dict, results: list[Result], warnings: list[str], generated: str) -> str:
    m = site["map"]
    by_name = {r.name: r for r in results}
    svg = [f'<svg viewBox="0 0 {m["width"]} {m["height"]}" role="img" aria-label="Site map with camera status">']
    perim = " ".join(f"{px},{py}" for px, py in m.get("perimeter", []))
    if perim:
        svg.append(f'<polygon points="{perim}" class="fence"/>')
    for a in m.get("areas", []):
        svg.append(f'<rect x="{a["x"]}" y="{a["y"]}" width="{a["w"]}" height="{a["h"]}" class="area"/>'
                   f'<text x="{a["x"] + 10}" y="{a["y"] + 22}" class="area-label">{html.escape(a["name"])}</text>')
    if m.get("gate"):
        (gx1, gy1), (gx2, gy2) = m["gate"]
        svg.append(f'<line x1="{gx1}" y1="{gy1}" x2="{gx2}" y2="{gy2}" class="gate"/>')
    for cam in site["cameras"]:
        r = by_name.get(cam["name"])
        colour = COLOURS[r.status] if r else COLOURS[UNKNOWN]
        title = html.escape(f'{cam.get("label", cam["name"])}: {r.status if r else "?"} - {r.reason if r else ""}')
        svg.append(f'<g><title>{title}</title>'
                   f'<path d="{_fov_path(cam)}" fill="{colour}" fill-opacity="0.18" stroke="{colour}" stroke-opacity="0.5"/>'
                   f'<circle cx="{cam["x"]}" cy="{cam["y"]}" r="9" fill="{colour}" class="cam"/>'
                   f'{_label(cam, m["width"])}</g>')
    svg.append("</svg>")

    counts = {s: sum(r.status == s for r in results) for s in COLOURS}
    legend = "".join(f'<span class="pill"><i style="background:{COLOURS[s]}"></i>{LABELS[s]} {counts[s]}</span>'
                     for s in COLOURS if counts[s] or s in (OK, OFFLINE))
    rows = "".join(
        f'<tr><td><i class="dot" style="background:{COLOURS[r.status]}"></i>{html.escape(r.label)}</td>'
        f'<td>{LABELS[r.status]}</td><td class="mono">{html.escape(r.ip)}</td><td>{html.escape(r.reason)}</td>'
        f'<td>{f"<a href={chr(34)}{RUNBOOK}#{r.runbook}{chr(34)}>runbook</a>" if r.runbook else ""}</td></tr>'
        for r in sorted(results, key=lambda r: (-SEVERITY[r.status], r.name)))
    warn_html = "".join(f'<p class="warn">⚠ {html.escape(w)}</p>' for w in warnings)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120"><title>Camera status - {html.escape(site['site']['name'])}</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#1c2230; --muted:#5d6675; --line:#d9dde4; --area:#eef1f5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --card:#1c2027; --ink:#e7eaf0; --muted:#9aa3b2; --line:#333a45; --area:#232832; }} }}
* {{ box-sizing:border-box }} body {{ margin:0; font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--ink) }}
main {{ max-width:1000px; margin:0 auto; padding:24px 16px 40px }}
h1 {{ font-size:22px; margin:0 0 4px }} .sub {{ color:var(--muted); margin:0 0 16px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px }}
svg {{ width:100%; height:auto; display:block }}
.fence {{ fill:none; stroke:var(--muted); stroke-width:2; stroke-dasharray:8 6 }}
.area {{ fill:var(--area); stroke:var(--line) }} .area-label {{ fill:var(--muted); font-size:14px }}
.gate {{ stroke:var(--bg); stroke-width:6 }} .cam {{ stroke:var(--card); stroke-width:3 }}
.cam-label {{ fill:var(--ink); font-size:13px; font-weight:600; paint-order:stroke; stroke:var(--card); stroke-width:4px }}
.pill {{ display:inline-flex; align-items:center; gap:6px; margin:0 14px 6px 0; font-size:14px }}
.pill i, .dot {{ display:inline-block; width:11px; height:11px; border-radius:50% }} .dot {{ margin-right:8px }}
table {{ width:100%; border-collapse:collapse; font-size:14px }} th, td {{ text-align:left; padding:8px 6px; border-top:1px solid var(--line); vertical-align:top }}
th {{ color:var(--muted); font-weight:600; border-top:0 }} .mono {{ font-family:ui-monospace,Menlo,monospace }}
.table-wrap {{ overflow-x:auto }} .warn {{ background:#fff4d6; color:#5c4300; padding:8px 12px; border-radius:8px; margin:0 0 12px }}
a {{ color:inherit }}
@media (max-width:600px) {{ th:nth-child(3), td:nth-child(3) {{ display:none }} .cam-label {{ font-size:16px }} }}
</style></head><body><main>
<h1>{html.escape(site['site']['name'])}: camera status</h1>
<p class="sub">Checked {html.escape(generated)} · refreshes every 2 minutes</p>
{warn_html}<div class="card">{legend}{''.join(svg)}</div>
<div class="card table-wrap"><table><tr><th>Camera</th><th>Status</th><th>IP</th><th>Detail</th><th></th></tr>{rows}</table></div>
</main></body></html>"""


# ---------- main ----------

def run(site_file: Path, out_dir: Path, frigate_api: str | None = None, rtsp_url: str | None = None,
        probe=probe_rtsp, now: dt.datetime | None = None) -> int:
    site = yaml.safe_load(site_file.read_text())
    cfg = site["site"]
    api = frigate_api or cfg["frigate_api"]
    template = rtsp_url or cfg["rtsp_url"]
    cams = site["cameras"]

    stats = fetch_frigate_stats(api)
    urls = [expand(template, ip=c.get("ip", ""), name=c["name"]) for c in cams]
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(probe, urls))
    results = [classify(c, p, stats, cfg.get("degraded_fps_ratio", 0.6)) for c, p in zip(cams, probes)]

    warnings = []
    if stats is None:
        warnings.append(f"Frigate API unreachable at {api}: recording status not verified")
    if (sw := storage_warning(stats, cfg.get("storage_warn_percent", 15))):
        warnings.append(sw)

    generated = (now or dt.datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M %Z")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "status.json").write_text(json.dumps(
        {"site": cfg["name"], "checked": generated, "warnings": warnings, "cameras": [asdict(r) for r in results]}, indent=2))
    (out_dir / "site_map.html").write_text(render_map(site, results, warnings, generated))

    for r in sorted(results, key=lambda r: (-SEVERITY[r.status], r.name)):
        hint = f"  [runbook: {r.runbook}]" if r.runbook else ""
        print(f"{r.status:<14} {r.name:<16} {r.ip:<12} {r.reason}{hint}")
    for w in warnings:
        print(f"WARNING        {w}")

    worst = max([SEVERITY[r.status] for r in results] + [1 if warnings else 0])
    return worst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", type=Path, default=HERE / "site.yaml")
    ap.add_argument("--out", type=Path, default=HERE.parent / "output")
    ap.add_argument("--frigate-api", help="override site.frigate_api")
    ap.add_argument("--rtsp-url", help="override site.rtsp_url, e.g. 'rtsp://127.0.0.1:8554/{name}/sub'")
    args = ap.parse_args()
    sys.exit(run(args.site, args.out, args.frigate_api, args.rtsp_url))


if __name__ == "__main__":
    main()
