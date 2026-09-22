"""Job model and in-memory/JSON-backed job store.

Linux reimplementation of the compiled ``jobs`` module shipped with the
HaramMute Windows desktop app. Exposes the interface ``main.py``,
``schemas.py`` and ``pipeline.py`` expect.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import settings

logger = logging.getLogger("harammute.jobs")

ACTIVE_STATUSES = frozenset({"queued", "downloading", "processing", "separating", "packaging"})


class JobStatus(str, Enum):
    queued = "queued"
    downloading = "downloading"
    processing = "processing"
    separating = "separating"
    packaging = "packaging"
    completed = "completed"
    failed = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    job_id: str
    source_url: str
    stems: int = 2
    owner_email: str = ""
    status: JobStatus = JobStatus.queued
    message: Optional[str] = None
    progress: Optional[float] = 0.0
    error_detail: Optional[str] = None
    failure_stage: Optional[str] = None
    failure_code: Optional[str] = None
    failure_recoverable: Optional[bool] = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    # Chunked processing
    chunking_enabled: bool = False
    total_duration: Optional[float] = None
    chunk_duration: float = 60.0
    chunks: List[dict] = field(default_factory=list)
    chunk_files: Dict[int, Path] = field(default_factory=dict)
    # Outputs
    stems_files: Dict[str, Path] = field(default_factory=dict)
    result_path: Optional[Path] = None
    processing_stats: Dict[str, object] = field(default_factory=dict)

    # ----- chunk helpers -------------------------------------------------
    def initialize_chunks(self, total_duration: float, chunk_duration: float = 60.0) -> None:
        """Initialize chunk metadata based on total duration."""
        self.total_duration = float(total_duration)
        self.chunk_duration = float(chunk_duration)
        self.chunking_enabled = True
        self.chunks = []
        self.chunk_files = {}
        start = 0.0
        index = 0
        while start < self.total_duration - 0.05:
            end = min(start + self.chunk_duration, self.total_duration)
            self.chunks.append(
                {
                    "index": index,
                    "status": "pending",
                    "progress": 0.0,
                    "start_time": round(start, 3),
                    "end_time": round(end, 3),
                    "duration": round(end - start, 3),
                    "file_ready": False,
                    "error": None,
                }
            )
            start = end
            index += 1
        if not self.chunks:
            self.chunks.append(
                {
                    "index": 0,
                    "status": "pending",
                    "progress": 0.0,
                    "start_time": 0.0,
                    "end_time": self.total_duration,
                    "duration": self.total_duration,
                    "file_ready": False,
                    "error": None,
                }
            )

    def update_chunk(self, chunk_index: int, **fields) -> None:
        """Update a specific chunk's status."""
        if 0 <= chunk_index < len(self.chunks):
            self.chunks[chunk_index].update(fields)
            self.updated_at = _now()

    def get_completed_chunks_count(self) -> int:
        return sum(1 for c in self.chunks if c.get("file_ready"))

    def get_first_ready_chunk(self) -> Optional[int]:
        for c in self.chunks:
            if c.get("file_ready"):
                return int(c["index"])
        return None

    def all_chunks_completed(self) -> bool:
        return bool(self.chunks) and all(c.get("status") == "completed" for c in self.chunks)

    # ----- persistence ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "source_url": self.source_url,
            "stems": self.stems,
            "owner_email": self.owner_email,
            "status": self.status.value,
            "message": self.message,
            "progress": self.progress,
            "error_detail": self.error_detail,
            "failure_stage": self.failure_stage,
            "failure_code": self.failure_code,
            "failure_recoverable": self.failure_recoverable,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "chunking_enabled": self.chunking_enabled,
            "total_duration": self.total_duration,
            "chunk_duration": self.chunk_duration,
            "chunks": self.chunks,
            "chunk_files": {str(k): str(v) for k, v in self.chunk_files.items()},
            "stems_files": {k: str(v) for k, v in self.stems_files.items()},
            "result_path": str(self.result_path) if self.result_path else None,
            "processing_stats": self.processing_stats,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        job = cls(
            job_id=data["job_id"],
            source_url=data["source_url"],
            stems=int(data.get("stems", 2)),
            owner_email=data.get("owner_email", ""),
            status=JobStatus(data.get("status", "queued")),
            message=data.get("message"),
            progress=data.get("progress"),
            error_detail=data.get("error_detail"),
            failure_stage=data.get("failure_stage"),
            failure_code=data.get("failure_code"),
            failure_recoverable=data.get("failure_recoverable"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            chunking_enabled=bool(data.get("chunking_enabled", False)),
            total_duration=data.get("total_duration"),
            chunk_duration=float(data.get("chunk_duration", 60.0)),
            chunks=list(data.get("chunks", [])),
            chunk_files={int(k): Path(v) for k, v in (data.get("chunk_files") or {}).items()},
            stems_files={k: Path(v) for k, v in (data.get("stems_files") or {}).items()},
            result_path=Path(data["result_path"]) if data.get("result_path") else None,
            processing_stats=dict(data.get("processing_stats") or {}),
        )
        return job


class JobStore:
    """Thread-safe job registry persisted as JSON under ``data_dir/jobs/<id>/job.json``."""

    def __init__(self, root: Optional[Path] = None):
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._last_poll: Dict[str, float] = {}
        self.root = Path(root) if root else settings.data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # ----- persistence ---------------------------------------------------
    def _job_file(self, job_id: str) -> Path:
        return self.root / job_id / "job.json"

    def _persist(self, job: Job) -> None:
        try:
            path = self._job_file(job.job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(job.to_dict(), indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Failed to persist job %s: %s", job.job_id, exc)

    def _load_existing(self) -> None:
        for job_dir in self.root.iterdir():
            f = job_dir / "job.json"
            if not f.is_file():
                continue
            try:
                job = Job.from_dict(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping unreadable job file %s: %s", f, exc)
                continue
            # Jobs interrupted by a restart can never finish: mark them failed.
            if job.status.value in ACTIVE_STATUSES:
                job.status = JobStatus.failed
                job.message = "Interrupted by app restart"
                job.error_detail = "Local worker stopped unexpectedly: app restarted"
                job.failure_stage = "processing_runtime"
                job.failure_code = "desktop_runtime_failed"
                job.failure_recoverable = True
                self._persist(job)
            self._jobs[job.job_id] = job
        if self._jobs:
            logger.info("Loaded %d persisted jobs", len(self._jobs))

    # ----- CRUD ----------------------------------------------------------
    def create_job(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.job_id] = job
            self._persist(job)
            return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update_job(self, job_id: str, **fields) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                if key == "status" and not isinstance(value, JobStatus):
                    value = JobStatus(value)
                setattr(job, key, value)
            job.updated_at = _now()
            self._persist(job)
            return job

    def save(self, job: Job) -> None:
        with self._lock:
            job.updated_at = _now()
            self._jobs[job.job_id] = job
            self._persist(job)

    # ----- client liveness ----------------------------------------------
    def touch_poll(self, job_id: str) -> None:
        """Record that a client just asked about this job."""
        import time

        self._last_poll[job_id] = time.monotonic()

    def is_abandoned(self, job_id: str, timeout_seconds: float) -> bool:
        """True when a client polled this job before but has gone quiet for ``timeout_seconds``."""
        import time

        last = self._last_poll.get(job_id)
        return last is not None and (time.monotonic() - last) > timeout_seconds

    # ----- queries -------------------------------------------------------
    @staticmethod
    def _matches(job: Job, source_url: str, stems: int, owner_email: str) -> bool:
        return (
            job.source_url == source_url
            and int(job.stems) == int(stems)
            and (job.owner_email or "").lower() == (owner_email or "").lower()
        )

    def find_cached_job(self, source_url: str, stems: int, owner_email: str) -> Optional[Job]:
        with self._lock:
            candidates = [
                j
                for j in self._jobs.values()
                if j.status == JobStatus.completed and self._matches(j, source_url, stems, owner_email)
            ]
            candidates.sort(key=lambda j: j.updated_at, reverse=True)
            for job in candidates:
                vocals = job.stems_files.get("vocals")
                chunks_ok = all(
                    job.chunk_files.get(int(c["index"])) and Path(job.chunk_files[int(c["index"])]).exists()
                    for c in job.chunks
                ) if job.chunking_enabled else True
                if vocals and Path(vocals).exists() and chunks_ok:
                    return job
            return None

    def find_active_job(self, source_url: str, stems: int, owner_email: str) -> Optional[Job]:
        with self._lock:
            for job in self._jobs.values():
                if job.status.value in ACTIVE_STATUSES and self._matches(job, source_url, stems, owner_email):
                    return job
            return None

    def create_or_reuse_active_job(self, job: Job) -> Tuple[Job, bool]:
        with self._lock:
            existing = self.find_active_job(job.source_url, job.stems, job.owner_email)
            if existing is not None:
                return existing, False
            self.create_job(job)
            return job, True

    def count_jobs_by_owner(self, owner_email: str, statuses: Iterable[JobStatus]) -> int:
        wanted = {s.value if isinstance(s, JobStatus) else str(s) for s in statuses}
        with self._lock:
            return sum(
                1
                for j in self._jobs.values()
                if (j.owner_email or "").lower() == (owner_email or "").lower() and j.status.value in wanted
            )

    def cleanup(self, cutoff: datetime) -> List[str]:
        """Forget finished jobs older than ``cutoff``; returns their ids so files can be removed."""
        removed: List[str] = []
        with self._lock:
            for job_id in list(self._jobs):
                job = self._jobs[job_id]
                if job.status.value in ACTIVE_STATUSES:
                    continue
                if job.updated_at < cutoff:
                    del self._jobs[job_id]
                    removed.append(job_id)
        return removed
