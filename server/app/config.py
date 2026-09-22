"""Settings for the HaramMute Linux server. All keys take the ``HARAMMUTE_`` prefix as env vars."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


def _default_data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "HaramMute"


class Settings(BaseSettings):
    version: str = "1.0.20"
    build_id: str = "linux"
    data_dir: Path = Field(default_factory=_default_data_dir)
    model_dir: Path | None = None
    model_filename: str = "UVR-MDX-NET-Voc_FT.onnx"
    port: int | None = None  # None: first free of 8765-8768

    ffmpeg_binary: str = "ffmpeg"
    encoding_quality: int = Field(default=4, description="MP3 VBR quality, 0 best to 9 worst")
    chunk_seconds: float = 30.0
    max_video_minutes: int = 90
    keep_hours: int = 24
    abandon_timeout_seconds: float = 90.0
    min_free_mb: int = Field(default=2000, description="Refuse a job when less RAM than this is available")

    cookies_file: Path | None = None
    browser_for_cookies: str | None = None
    skip_browser_cookies: bool = False

    ort_providers: str | None = Field(
        default=None,
        description="Comma-separated ONNX Runtime providers to force, e.g. OpenVINOExecutionProvider,CPUExecutionProvider",
    )

    model_config = {"env_prefix": "HARAMMUTE_"}

    @property
    def max_video_seconds(self) -> int:
        return self.max_video_minutes * 60


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
if settings.cookies_file and not settings.cookies_file.exists():
    raise FileNotFoundError(f"HARAMMUTE_COOKIES_FILE {settings.cookies_file} does not exist")
