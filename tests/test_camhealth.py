"""Tests for healthcheck/camhealth.py.

Unit tests use fake probes and the Frigate stats fixture (same structure as Frigate 0.18's /api/stats).
The integration test runs real RTSP streams from the MediaMTX simulator (sim/mediamtx.yml) and a fake
Frigate API; it is skipped if the mediamtx binary or ffprobe isn't installed.
    pip install pyyaml pytest && pytest -v tests/
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "healthcheck"))
import camhealth as ch  # noqa: E402

STATS = json.loads((ROOT / "tests/fixtures/frigate_stats.json").read_text())
CAM = {"name": "gate_entrance", "label": "Gate entrance", "ip": "10.30.0.11", "x": 100, "y": 100, "heading": 90}
GOOD = ch.Probe(ok=True, codec="h264", width=640, height=360, fps=5.0)


# ---- classification ----

def test_ok_when_stream_and_frigate_healthy():
    r = ch.classify(CAM, GOOD, STATS, 0.6)
    assert r.status == ch.OK and r.runbook == ""

@pytest.mark.parametrize("error, cause", [
    ("method DESCRIBE failed: 401 Unauthorized", "authentication failed"),
    ("method DESCRIBE failed: 404 Not Found", "stream path not found"),
    ("Connection to tcp://10.30.0.16:554 failed: Connection refused", "connection refused"),
    ("Connection to tcp://10.30.0.16:554 failed: Connection timed out", "no response"),
])
def test_offline_reason_from_ffprobe_error(error, cause):
    r = ch.classify(CAM, ch.Probe(ok=False, error=error), STATS, 0.6)
    assert r.status == ch.OFFLINE and cause in r.reason and r.runbook == "camera-offline"

def test_not_receiving_when_stream_up_but_frigate_has_no_frames():
    cam = dict(CAM, name="warehouse_aisle")
    r = ch.classify(cam, GOOD, STATS, 0.6)
    assert r.status == ch.NOT_RECEIVING and r.runbook == "stream-ok-but-frigate-not-receiving"

def test_not_receiving_when_camera_missing_from_frigate():
    r = ch.classify(dict(CAM, name="new_camera"), GOOD, STATS, 0.6)
    assert r.status == ch.NOT_RECEIVING and "not in Frigate" in r.reason

def test_degraded_on_low_fps_with_reconnects():
    r = ch.classify(dict(CAM, name="yard_north"), GOOD, STATS, 0.6)
    assert r.status == ch.DEGRADED and "2.1 of 5 fps" in r.reason and "7 reconnects" in r.reason

def test_unknown_when_frigate_unreachable():
    assert ch.classify(CAM, GOOD, None, 0.6).status == ch.UNKNOWN

def test_offline_takes_priority_over_frigate_unreachable():
    assert ch.classify(CAM, ch.Probe(ok=False, error="timed out"), None, 0.6).status == ch.OFFLINE


# ---- helpers ----

def test_storage_warning_below_threshold():
    w = ch.storage_warning(STATS, 15)
    assert w and w.startswith("Recordings disk 12% free")
    assert ch.storage_warning(STATS, 10) is None

def test_credentials_masked():
    assert ch.mask_credentials("rtsp://nvr:s3cret@10.30.0.11:554/stream2") == "rtsp://nvr:***@10.30.0.11:554/stream2"

def test_template_expansion(monkeypatch):
    monkeypatch.setenv("CAM_RTSP_USER", "nvr")
    monkeypatch.setenv("CAM_RTSP_PASSWORD", "pw")
    url = ch.expand("rtsp://${CAM_RTSP_USER}:${CAM_RTSP_PASSWORD}@{ip}:554/stream2", ip="10.30.0.11", name="x")
    assert url == "rtsp://nvr:pw@10.30.0.11:554/stream2"

def test_fov_points_along_heading():
    # heading 90 = east: both edges of the cone are to the right of the camera
    path = ch._fov_path({"x": 100, "y": 100, "heading": 90, "fov": 60, "range": 50})
    xs = [float(p.split(",")[0]) for p in path.replace("M", "").replace("L", " ").split()[1:2]]
    assert xs[0] > 100

def test_map_escapes_labels():
    site = {"site": {"name": "Test <site>"}, "map": {"width": 100, "height": 100}, "cameras": [dict(CAM, label="A&B")]}
    out = ch.render_map(site, [ch.classify(dict(CAM, label="A&B"), GOOD, STATS, 0.6)], [], "now")
    assert "A&amp;B" in out and "Test &lt;site&gt;" in out and "<script" not in out


# ---- full run with fake probes ----

def test_run_exit_codes(tmp_path, monkeypatch):
    def fake_stats(api, timeout_s=5):
        return STATS
    monkeypatch.setattr(ch, "fetch_frigate_stats", fake_stats)
    all_up = lambda url: GOOD  # noqa: E731
    code = ch.run(ROOT / "healthcheck/site.yaml", tmp_path, rtsp_url="rtsp://x/{name}", probe=all_up)
    assert code == 2  # warehouse_aisle and parking_lot report no frames in the fixture
    status = json.loads((tmp_path / "status.json").read_text())
    assert len(status["cameras"]) == 6 and (tmp_path / "site_map.html").exists()


# ---- integration: real RTSP streams ----

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

@pytest.fixture(scope="module")
def simulator():
    binary = shutil.which("mediamtx") or os.environ.get("MEDIAMTX")
    if not binary or not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        pytest.skip("mediamtx/ffmpeg not installed")
    port = _free_port()
    env = dict(os.environ, MTX_RTSPADDRESS=f":{port}")
    proc = subprocess.Popen([binary, str(ROOT / "sim/mediamtx.yml")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline and not ch.probe_rtsp(f"rtsp://127.0.0.1:{port}/office_lobby/sub", 2).ok:
        time.sleep(1)
    yield port
    proc.terminate()
    proc.wait(timeout=10)

@pytest.fixture(scope="module")
def fake_frigate():
    body = json.dumps(STATS).encode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/api/stats"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body if ok else b"{}")
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api"
    srv.shutdown()

def test_probe_real_stream(simulator):
    p = ch.probe_rtsp(f"rtsp://127.0.0.1:{simulator}/gate_entrance/main")
    assert p.ok and p.codec == "h264" and (p.width, p.height) == (1280, 720) and p.fps == 10

def test_probe_missing_stream(simulator):
    p = ch.probe_rtsp(f"rtsp://127.0.0.1:{simulator}/parking_lot/sub")
    assert not p.ok

def test_end_to_end(simulator, fake_frigate, tmp_path):
    code = ch.run(ROOT / "healthcheck/site.yaml", tmp_path, frigate_api=fake_frigate,
                  rtsp_url=f"rtsp://127.0.0.1:{simulator}/{{name}}/sub")
    got = {c["name"]: c["status"] for c in json.loads((tmp_path / "status.json").read_text())["cameras"]}
    assert got == {
        "gate_entrance": "OK", "loading_dock": "OK", "office_lobby": "OK",
        "yard_north": "DEGRADED",              # streaming, but Frigate gets 2.1 of 5 fps
        "warehouse_aisle": "NOT_RECEIVING",    # streaming, but Frigate gets nothing (config/credentials)
        "parking_lot": "OFFLINE",              # no stream at all
    }
    assert code == 2
