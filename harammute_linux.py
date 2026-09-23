#!/usr/bin/env python3
"""Linux server for the HaramMute browser extension (third-party, independent project).

Drop-in replacement for the Windows-only "HaramMute desktop app" that the
HaramMute browser extension talks to in "On Your Computer" mode.
Serves the same HTTP API on 127.0.0.1:8765 (falling back to 8766-8768).
"""

import logging
import os
import shutil
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FALLBACK_PORTS = [8765, 8766, 8767, 8768]

# Bundled model dir is the default; settings can override via HARAMMUTE_MODEL_DIR.
os.environ.setdefault("HARAMMUTE_MODEL_DIR", str(BASE_DIR / "model_cache" / "audio-separator"))
sys.path.insert(0, str(BASE_DIR / "server"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("harammute")


def pick_port(forced: int | None) -> int:
    if forced:
        return forced
    for port in FALLBACK_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"All ports {FALLBACK_PORTS} are busy")


def main() -> None:
    from app.config import settings

    for tool in (settings.ffmpeg_binary, "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")
    if not shutil.which("deno"):
        logger.warning("deno not found: yt-dlp may fail on some YouTube videos. Install it (e.g. pacman -S deno).")

    import uvicorn
    from app.api import app, set_bound_address
    from app.local_processing import onnx_providers, preferred_providers

    host, port = "127.0.0.1", pick_port(settings.port)
    set_bound_address(host, port)
    logger.info("ONNX Runtime providers available: %s, using: %s", onnx_providers(), preferred_providers())
    logger.info("HaramMute Linux server v%s -> http://%s:%d (data: %s)", settings.version, host, port, settings.data_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
