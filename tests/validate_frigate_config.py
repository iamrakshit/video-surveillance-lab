"""Validate frigate/config.yml and config.sim.yml with Frigate's own config model.

Frigate isn't on PyPI, so this uses a checkout of its source:
    git clone --depth 1 --branch v0.18.2 https://github.com/blakeblackshear/frigate.git /opt/frigate-src
    echo 'VERSION = "0.18.2"' > /opt/frigate-src/frigate/version.py
    pip install pydantic numpy opencv-python-headless ruamel.yaml pyyaml requests psutil py3nvml unidecode
    FRIGATE_SRC=/opt/frigate-src python tests/validate_frigate_config.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("FRIGATE_RTSP_USER", "nvr")
os.environ.setdefault("FRIGATE_RTSP_PASSWORD", "example")
sys.path.insert(0, os.environ.get("FRIGATE_SRC", "/opt/frigate-src"))
from frigate.config import FrigateConfig  # noqa: E402

root = Path(__file__).resolve().parent.parent
failed = False
for name in ("config.yml", "config.sim.yml"):
    try:
        cfg = FrigateConfig.parse_yaml((root / "frigate" / name).read_text())
        cams = cfg.cameras
        print(f"ok   {name}: {len(cams)} cameras, record={cfg.record.enabled}, "
              f"motion retain {cfg.record.motion.days} d, alerts {cfg.record.alerts.retain.days} d")
        for cam in cams.values():
            roles = sorted(r for i in cam.ffmpeg.inputs for r in i.roles)
            assert roles == ["detect", "record"], f"{cam.name}: roles {roles}"
    except Exception as exc:  # noqa: BLE001
        failed = True
        print(f"FAIL {name}: {exc}")
sys.exit(1 if failed else 0)
