"""HTTP API compatible with the HaramMute browser extension's local mode."""

from __future__ import annotations

import logging
import platform
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib import metadata

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import settings
from .jobs import Job, JobStatus, JobStore
from .models import JobCreateRequest, JobResultResponse, JobStatusResponse

logger = logging.getLogger("harammute.api")

app = FastAPI(title="HaramMute Linux server", version=settings.version)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

job_store = JobStore()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="harammute-job")
_bound = {"host": "127.0.0.1", "port": 8765}


def set_bound_address(host: str, port: int) -> None:
    _bound.update(host=host, port=port)


# ----------------------------------------------------------------- identity
def _dep(packages=(), binary=None) -> dict:
    version, status = "unknown", "missing"
    for name in packages:
        try:
            version = metadata.version(name)
            status = "available"
            break
        except metadata.PackageNotFoundError:
            continue
    if binary and shutil.which(binary):
        status = "available"
    return {"status": status, "version": version}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": settings.version}


@app.get("/client-info")
async def client_info() -> dict:
    from .local_processing import onnx_providers

    return {
        "status": "ok",
        "client": "harammute-desktop",
        "desktop_app_version": settings.version,
        "build_id": settings.build_id,
        "install_channel": "linux",
        "platform": platform.system().lower(),
        "os": sys.platform,
        "arch": platform.machine().lower(),
        "local_api_version": "1",
        "local_api_protocol": "http",
        "local_api_host": _bound["host"],
        "local_api_port": _bound["port"],
        "runtime": {"python_version": platform.python_version(), "onnx_providers": onnx_providers()},
        "dependencies": {
            "yt_dlp": _dep(["yt-dlp"]),
            "deno": _dep([], "deno"),
            "ffmpeg": _dep([], settings.ffmpeg_binary),
            "audio_separator": _dep(["audio-separator"]),
            "torch": _dep(["torch"]),
            "onnxruntime": _dep(["onnxruntime-gpu", "onnxruntime-openvino", "onnxruntime"]),
        },
    }


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>HaramMute</title>"
        "<body style='font-family:system-ui;padding:2rem'><h1>HaramMute local server is running</h1>"
        f"<p>Version {settings.version} on port {_bound['port']}. "
        "Pick <b>On Your Computer</b> in the extension popup.</p></body>"
    )


# ----------------------------------------------------------------- jobs
def _run(job_id: str) -> None:
    from .pipeline import process_job

    try:
        process_job(job_id, job_store)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s crashed", job_id)
        job_store.update_job(
            job_id,
            status=JobStatus.failed,
            message="Processing stopped unexpectedly",
            error_detail=f"Local worker stopped unexpectedly: {type(exc).__name__}: {exc}",
            failure_stage="processing_runtime",
            failure_code="desktop_runtime_failed",
            failure_recoverable=False,
        )


def _cleanup_expired() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.keep_hours)
    for job_id in job_store.cleanup(cutoff):
        shutil.rmtree(settings.data_dir / "jobs" / job_id, ignore_errors=True)


@app.post("/jobs", response_model=JobStatusResponse, status_code=202)
async def create_job(payload: JobCreateRequest) -> JobStatusResponse:
    url = str(payload.url)
    if not payload.skip_cache:
        existing = job_store.find_active_job(url, payload.stems, "") or job_store.find_cached_job(url, payload.stems, "")
        if existing is not None:
            logger.info("Reusing job %s (%s) for %s", existing.job_id, existing.status.value, url)
            return JobStatusResponse.from_job(existing)

    job = Job(job_id=uuid.uuid4().hex, source_url=url, stems=payload.stems, message="Queued", progress=0.0)
    job, created = job_store.create_or_reuse_active_job(job)
    if created:
        _executor.submit(_run, job.job_id)
        _cleanup_expired()
    return JobStatusResponse.from_job(job)


@app.get("/jobs/{job_id}", response_model=JobResultResponse)
async def get_job(job_id: str) -> JobResultResponse:
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_store.touch_poll(job_id)
    return JobResultResponse.from_job(job)


@app.get("/jobs/{job_id}/stems/{stem_name}")
async def download_stem(job_id: str, stem_name: str) -> FileResponse:
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    path = job.stems_files.get(stem_name.lower())
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Stem not found")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/jobs/{job_id}/chunks/{chunk_index}/vocals")
async def download_chunk(job_id: str, chunk_index: int):
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job_store.touch_poll(job_id)
    if not job.chunking_enabled:
        raise HTTPException(status_code=400, detail="Job does not use chunked processing")
    if chunk_index < 0 or chunk_index >= len(job.chunks):
        raise HTTPException(status_code=404, detail="Chunk not found")
    chunk = job.chunks[chunk_index]
    path = job.chunk_files.get(chunk_index)
    if not chunk.get("file_ready") or not path or not path.exists():
        return JSONResponse(status_code=202, content={"detail": "Chunk not ready", "status": chunk.get("status")})
    return FileResponse(path, media_type="audio/mpeg")


@app.on_event("shutdown")
async def _shutdown() -> None:
    _executor.shutdown(wait=False)
