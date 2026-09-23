"""Local processing helpers: yt-dlp download and UVR (audio-separator) separation.

Separation runs in a short-lived child process (one per job). The model and
all of torch/onnxruntime's arenas live only in that child, so memory goes back
to the OS as soon as the job finishes or is cancelled.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from .config import settings

logger = logging.getLogger("harammute.local")

MODEL_FILENAME = settings.model_filename
SAMPLE_RATE = 44100

ProgressCallback = Callable[[float, str], None]


# --------------------------------------------------------------------------
# Paths / tools
# --------------------------------------------------------------------------
def model_dir() -> Path:
    return Path(settings.model_dir) if settings.model_dir else settings.data_dir / "models" / "audio-separator"


def available_memory_mb() -> Optional[int]:
    """MemAvailable from /proc/meminfo, or None when unknown."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def preferred_providers() -> List[str]:
    """Execution providers to use, best first. Honors HARAMMUTE_ORT_PROVIDERS."""
    available = onnx_providers()
    if settings.ort_providers:
        wanted = [p.strip() for p in settings.ort_providers.split(",") if p.strip()]
        chosen = [p for p in wanted if p in available]
        if chosen:
            return chosen
        logger.warning("None of HARAMMUTE_ORT_PROVIDERS=%s are available (%s); using CPU", wanted, available)
    for candidate in ("CUDAExecutionProvider", "ROCMExecutionProvider", "OpenVINOExecutionProvider"):
        if candidate in available:
            return [candidate, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def ffmpeg_bin() -> str:
    return shutil.which(settings.ffmpeg_binary) or settings.ffmpeg_binary


def ffprobe_bin() -> str:
    candidate = str(Path(ffmpeg_bin()).with_name("ffprobe"))
    return candidate if Path(candidate).exists() else (shutil.which("ffprobe") or "ffprobe")


def run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    logger.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def audio_duration(path: Path) -> float:
    out = run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    ).stdout
    return float(json.loads(out)["format"]["duration"])


# --------------------------------------------------------------------------
# yt-dlp
# --------------------------------------------------------------------------
class DownloadError(RuntimeError):
    def __init__(self, message: str, *, recoverable: bool = True):
        super().__init__(message)
        self.recoverable = recoverable


def detect_browser_for_cookies() -> List[str]:
    """Return browser names yt-dlp can pull cookies from, most likely first."""
    if settings.browser_for_cookies:
        return [settings.browser_for_cookies]
    home = Path.home()
    candidates = [
        ("brave", home / ".config/BraveSoftware/Brave-Browser"),
        ("chrome", home / ".config/google-chrome"),
        ("chromium", home / ".config/chromium"),
        ("firefox", home / ".mozilla/firefox"),
        ("vivaldi", home / ".config/vivaldi"),
    ]
    return [name for name, path in candidates if path.exists()]


def _yt_dlp_opts(out_dir: Path, cookies_browser: Optional[str], progress: Optional[ProgressCallback]):
    def hook(d):
        if progress is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            progress((done / total) if total else 0.0, "Downloading audio...")
        elif d.get("status") == "finished":
            progress(1.0, "Download finished")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if settings.cookies_file:
        opts["cookiefile"] = str(settings.cookies_file)
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def _classify_yt_dlp_error(text: str) -> tuple[str, bool]:
    """Map yt-dlp failure text to the extension's error vocabulary."""
    t = text.lower()
    if "sign in to confirm" in t or "bot" in t:
        return "bot_check", True
    if "age" in t and ("confirm" in t or "restrict" in t):
        return "age_restricted", False
    if "private video" in t or "unavailable" in t or "removed" in t:
        return "video_unavailable", False
    if "members-only" in t or "join this channel" in t or "premieres" in t:
        return "members_only", False
    if "not available in your country" in t or "geo" in t:
        return "geo_blocked", False
    if "live" in t and "stream" in t:
        return "live_stream", False
    return "download_failed", True


def download_audio(url: str, out_dir: Path, progress: Optional[ProgressCallback] = None) -> Path:
    """Download best audio for ``url`` into ``out_dir`` and return the file path."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    attempts: List[Optional[str]] = [None]
    if not settings.skip_browser_cookies and not settings.cookies_file:
        attempts.extend(detect_browser_for_cookies())

    last_error: Optional[str] = None
    for browser in attempts:
        opts = _yt_dlp_opts(out_dir, browser, progress)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            if not path.exists():
                matches = sorted(out_dir.glob("source.*"))
                if not matches:
                    raise DownloadError("yt-dlp finished but no file was produced")
                path = matches[0]
            logger.info("Downloaded %s (%s)", path.name, "no cookies" if browser is None else f"cookies from {browser}")
            return path
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            code, recoverable = _classify_yt_dlp_error(last_error)
            logger.warning("yt-dlp failed (%s, cookies=%s): %s", code, browser, last_error.splitlines()[0][:300])
            if code != "bot_check" or browser == attempts[-1]:
                raise DownloadError(last_error, recoverable=recoverable) from exc
            continue  # bot check without cookies: retry with the next browser's cookies
    raise DownloadError(last_error or "Download failed")


def convert_to_wav(src: Path, dst: Path) -> Path:
    """Convert any audio/video file to 44.1 kHz stereo 16-bit WAV."""
    run(
        [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
         "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)]
    )
    return dst


# --------------------------------------------------------------------------
# audio-separator, in a child process
# --------------------------------------------------------------------------
def onnx_providers() -> List[str]:
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        return []


def _worker_main(conn, model_dir_s: str, model_filename: str, providers: List[str]) -> None:
    """Child process: load the model once, then separate files on request."""
    import logging as _logging

    _logging.basicConfig(level=_logging.WARNING)
    try:
        from audio_separator.separator import Separator

        sep = Separator(
            log_level=_logging.WARNING,
            model_file_dir=model_dir_s,
            output_dir=model_dir_s,  # replaced per request
            output_format="WAV",
            output_single_stem="Vocals",
            sample_rate=SAMPLE_RATE,
            use_autocast=False,
            mdx_params={"hop_length": 1024, "segment_size": 256, "overlap": 0.25, "batch_size": 1, "enable_denoise": False},
        )
        sep.onnx_execution_provider = providers  # audio-separator only auto-picks CUDA/CoreML/DirectML
        sep.load_model(model_filename=model_filename)
        conn.send(("ready", providers))
    except Exception as exc:  # noqa: BLE001
        conn.send(("fatal", f"{type(exc).__name__}: {exc}"))
        return

    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        wav_path, output_dir, stem_name = msg
        try:
            sep.output_dir = output_dir
            if sep.model_instance is not None:
                sep.model_instance.output_dir = output_dir
            outputs = sep.separate(wav_path, custom_output_names={"Vocals": stem_name})
            found = None
            for out in outputs or []:
                p = Path(out)
                if not p.is_absolute():
                    p = Path(output_dir) / p
                if p.exists():
                    found = str(p)
                    break
            if found is None:
                conn.send(("error", f"audio-separator did not produce output: {outputs}"))
            else:
                conn.send(("ok", found))
        except Exception as exc:  # noqa: BLE001
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class SeparatorWorker:
    """One model-loaded child process, used for the chunks of a single job."""

    def __init__(self):
        ctx = mp.get_context("spawn")
        self._conn, child_conn = ctx.Pipe()
        mdir = model_dir()
        mdir.mkdir(parents=True, exist_ok=True)
        if not (mdir / MODEL_FILENAME).exists():
            logger.warning("Model %s not found in %s; audio-separator will download it", MODEL_FILENAME, mdir)
        providers = preferred_providers()
        self._proc = ctx.Process(target=_worker_main, args=(child_conn, str(mdir), MODEL_FILENAME, providers), daemon=True)
        self._proc.start()
        child_conn.close()
        status, payload = self._conn.recv()
        if status != "ready":
            self.close()
            raise RuntimeError(f"separation worker failed to start: {payload}")
        logger.info("Separation worker pid %s ready (model %s, providers=%s)", self._proc.pid, MODEL_FILENAME, payload)

    def separate(self, wav_path: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._conn.send((str(wav_path), str(output_dir), f"{wav_path.stem}_vocals"))
        status, payload = self._conn.recv()
        if status != "ok":
            raise RuntimeError(payload)
        return Path(payload)

    def close(self) -> None:
        try:
            self._conn.send(None)
        except Exception:  # noqa: BLE001
            pass
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join(timeout=5)
        self._conn.close()
        logger.info("Separation worker pid %s exited", self._proc.pid)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
