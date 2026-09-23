"""Job pipeline: download -> split into chunks -> separate vocals -> encode MP3.

Produces per-chunk vocals MP3s for progressive playback plus a full ``vocals``
stem once every chunk is done, in the layout the browser extension polls for.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from .config import settings
from .jobs import JobStatus, JobStore
from .local_processing import (
    DownloadError,
    SeparatorWorker,
    audio_duration,
    available_memory_mb,
    convert_to_wav,
    download_audio,
    ffmpeg_bin,
    run,
)

logger = logging.getLogger("harammute.pipeline")

# Extra audio fed to the model on each side of a chunk so the cut edges are
# separated with context, then trimmed away again before encoding.
EDGE_PAD_SECONDS = 1.5
# One separation model in memory at a time; extra jobs wait their turn.
_job_slot = threading.Semaphore(1)


def _split_audio_chunk(source_wav: Path, dst: Path, start: float, duration: float) -> tuple[float, float]:
    """Split a padded chunk from the source WAV. Returns (lead_pad, actual_duration)."""
    lead = min(EDGE_PAD_SECONDS, start)
    total = audio_duration(source_wav)
    trail = min(EDGE_PAD_SECONDS, max(0.0, total - (start + duration)))
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            f"{start - lead:.3f}",
            "-t",
            f"{duration + lead + trail:.3f}",
            "-i",
            str(source_wav),
            "-c:a",
            "pcm_s16le",
            str(dst),
        ]
    )
    return lead, duration


def _trim_and_encode(vocals_wav: Path, dst_mp3: Path, dst_wav: Path, lead: float, duration: float) -> None:
    """Trim the padding back off and write both a trimmed WAV and an MP3."""
    base = [
        ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{lead:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(vocals_wav),
    ]
    run(base + ["-c:a", "pcm_s16le", str(dst_wav)])
    run(base + ["-c:a", "libmp3lame", "-q:a", str(settings.encoding_quality), str(dst_mp3)])


def _assemble_chunked_vocals(chunk_wavs: list[Path], dst_mp3: Path, work_dir: Path) -> None:
    """Concatenate trimmed chunk WAVs into one MP3 stem."""
    list_file = work_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in chunk_wavs), encoding="utf-8")
    run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c:a",
            "libmp3lame",
            "-q:a",
            str(settings.encoding_quality),
            str(dst_mp3),
        ]
    )


def _fail(job_store: JobStore, job_id: str, message: str, detail: str, stage: str, code: str, recoverable: bool):
    logger.error("Job %s failed at %s: %s", job_id, stage, detail.splitlines()[0][:300] if detail else message)
    job_store.update_job(
        job_id,
        status=JobStatus.failed,
        message=message,
        error_detail=detail,
        failure_stage=stage,
        failure_code=code,
        failure_recoverable=recoverable,
    )


def process_job(job_id: str, job_store: JobStore) -> None:
    with _job_slot:
        _process_job(job_id, job_store)


def _process_job(job_id: str, job_store: JobStore) -> None:
    job = job_store.get_job(job_id)
    if job is None:
        logger.error("process_job: unknown job %s", job_id)
        return

    job_dir = settings.data_dir / "jobs" / job_id
    work = job_dir / "work"
    result = job_dir / "result"
    work.mkdir(parents=True, exist_ok=True)
    result.mkdir(parents=True, exist_ok=True)
    stats: dict = {"started_at": time.time()}

    free_mb = available_memory_mb()
    if free_mb is not None and free_mb < settings.min_free_mb:
        _fail(
            job_store, job_id, "Not enough free memory",
            f"Only {free_mb} MB RAM available; separation needs about {settings.min_free_mb} MB. Close some apps and retry.",
            "validate", "low_memory", True,
        )
        return

    # ------------------------------------------------------------------ download
    job_store.update_job(job_id, status=JobStatus.downloading, message="Downloading audio...", progress=0.0)

    def dl_progress(frac: float, msg: str):
        job_store.update_job(job_id, message=msg, progress=round(0.05 * frac, 4))

    t0 = time.time()
    try:
        downloaded = download_audio(job.source_url, work, dl_progress)
    except DownloadError as exc:
        _fail(job_store, job_id, "Couldn't download this video", str(exc), "download", "download_failed", exc.recoverable)
        return
    except Exception as exc:  # noqa: BLE001
        _fail(job_store, job_id, "Couldn't download this video", f"{type(exc).__name__}: {exc}", "download", "download_failed", True)
        return
    stats["download_seconds"] = round(time.time() - t0, 2)

    source_wav = work / "source.wav"
    try:
        convert_to_wav(downloaded, source_wav)
        total = audio_duration(source_wav)
    except Exception as exc:  # noqa: BLE001
        _fail(job_store, job_id, "Couldn't decode audio", f"{type(exc).__name__}: {exc}", "decode", "decode_failed", False)
        return

    if total > settings.max_video_seconds:
        _fail(
            job_store,
            job_id,
            "Video is too long",
            f"Video is too long ({total / 60:.1f} min; limit {settings.max_video_seconds // 60} min)",
            "validate",
            "video_too_long",
            False,
        )
        return

    # ------------------------------------------------------------------ chunk plan
    job = job_store.get_job(job_id)
    job.initialize_chunks(total, settings.chunk_seconds)
    job.processing_stats = stats
    job_store.save(job)
    n = len(job.chunks)
    job_store.update_job(job_id, status=JobStatus.separating, message=f"Processing part 1 of {n}...", progress=0.05)
    logger.info("Job %s: %.1fs audio, %d chunks", job_id, total, n)

    # ------------------------------------------------------------------ separate each chunk
    chunk_wavs: list[Path] = []
    sep_seconds = 0.0
    try:
        worker = SeparatorWorker()
    except Exception as exc:  # noqa: BLE001
        _fail(job_store, job_id, "Couldn't start audio processing", f"{type(exc).__name__}: {exc}", "separation", "desktop_runtime_failed", False)
        return
    try:
        if not _separate_all_chunks(job_id, job_store, job, worker, source_wav, work, result, chunk_wavs):
            return
    finally:
        worker.close()

    # ------------------------------------------------------------------ package full stem
    _package(job_id, job_store, chunk_wavs, work, result, source_wav, stats)


def _separate_all_chunks(job_id, job_store, job, worker, source_wav, work, result, chunk_wavs) -> bool:
    n = len(job.chunks)
    sep_seconds = 0.0
    for chunk in job.chunks:
        i = int(chunk["index"])
        start = float(chunk["start_time"])
        duration = float(chunk["duration"])
        chunk_wav = work / f"chunk_{i}.wav"
        vocals_mp3 = result / f"vocals_chunk_{i}.mp3"
        vocals_trim_wav = work / f"vocals_chunk_{i}.wav"

        if job_store.is_abandoned(job_id, settings.abandon_timeout_seconds):
            logger.info("Job %s abandoned by client after %d/%d chunks; stopping", job_id, i, n)
            _fail(job_store, job_id, "Cancelled", "Client stopped polling; processing cancelled", "separation", "cancelled", True)
            return False
        job = job_store.get_job(job_id)
        job.update_chunk(i, status="separating", progress=0.1)
        job_store.save(job)
        try:
            t1 = time.time()
            lead, dur = _split_audio_chunk(source_wav, chunk_wav, start, duration)
            vocals_wav = worker.separate(chunk_wav, work / "sep")
            _trim_and_encode(vocals_wav, vocals_mp3, vocals_trim_wav, lead, dur)
            sep_seconds += time.time() - t1
            chunk_wav.unlink(missing_ok=True)
            vocals_wav.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            job = job_store.get_job(job_id)
            job.update_chunk(i, status="failed", error=f"{type(exc).__name__}: {exc}")
            job_store.save(job)
            _fail(
                job_store,
                job_id,
                f"Part {i + 1} failed to process",
                f"{type(exc).__name__}: {exc}",
                "separation",
                "separation_failed",
                True,
            )
            return False

        chunk_wavs.append(vocals_trim_wav)
        job = job_store.get_job(job_id)
        job.chunk_files[i] = vocals_mp3
        job.update_chunk(i, status="completed", progress=1.0, file_ready=True)
        done = i + 1
        job.progress = round(0.05 + 0.9 * (done / n), 4)
        job.message = f"Part {done} of {n} ready" if done < n else "All parts ready"
        job.processing_stats["separation_seconds"] = round(sep_seconds, 2)
        job.processing_stats["realtime_factor"] = round(sep_seconds / max(1e-6, start + duration), 3)
        job_store.save(job)
        logger.info("Job %s: chunk %d/%d done (%.1fs so far)", job_id, done, n, sep_seconds)
    return True


def _package(job_id, job_store, chunk_wavs, work, result, source_wav, stats) -> None:
    job_store.update_job(job_id, status=JobStatus.packaging, message="Packaging...", progress=0.97)
    try:
        full = result / "vocals.mp3"
        _assemble_chunked_vocals(chunk_wavs, full, work)
    except Exception as exc:  # noqa: BLE001
        _fail(job_store, job_id, "Failed to package audio", f"{type(exc).__name__}: {exc}", "packaging", "packaging_failed", True)
        return

    for p in chunk_wavs:
        p.unlink(missing_ok=True)
    shutil.rmtree(work / "sep", ignore_errors=True)
    source_wav.unlink(missing_ok=True)

    job = job_store.get_job(job_id)
    job.stems_files = {"vocals": full}
    job.status = JobStatus.completed
    job.progress = 1.0
    job.message = "Completed"
    job.processing_stats["total_seconds"] = round(time.time() - stats["started_at"], 2)
    job_store.save(job)
    logger.info("Job %s completed in %.1fs", job_id, job.processing_stats["total_seconds"])
