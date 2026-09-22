"""Wire models for the API the HaramMute extension speaks."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl

from .jobs import Job, JobStatus


class ChunkInfo(BaseModel):
    index: int
    status: str = "pending"
    progress: float = 0.0
    start_time: float
    end_time: float
    duration: float
    file_ready: bool = False
    error: Optional[str] = None


class JobCreateRequest(BaseModel):
    url: HttpUrl
    stems: Literal[2] = 2
    skip_cache: bool = False


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    stems: int
    message: Optional[str] = None
    progress: Optional[float] = None
    error_detail: Optional[str] = None
    failure_stage: Optional[str] = None
    failure_code: Optional[str] = None
    failure_recoverable: Optional[bool] = None
    created_at: datetime
    updated_at: datetime
    chunking_enabled: bool = False
    total_duration: Optional[float] = None
    chunk_duration: float = 30.0
    total_chunks: int = 0
    chunks_completed: int = 0
    chunks: List[ChunkInfo] = Field(default_factory=list)
    processing_stats: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_job(cls, job: Job) -> "JobStatusResponse":
        chunks = [ChunkInfo(**c) for c in job.chunks]
        # Only count the contiguous ready run from index 0, so the client never skips a gap.
        completed = 0
        for c in chunks:
            if not c.file_ready:
                break
            completed += 1
        return cls(
            job_id=job.job_id,
            status=job.status,
            stems=job.stems,
            message=job.message,
            progress=job.progress,
            error_detail=job.error_detail,
            failure_stage=job.failure_stage,
            failure_code=job.failure_code,
            failure_recoverable=job.failure_recoverable,
            created_at=job.created_at,
            updated_at=job.updated_at,
            chunking_enabled=job.chunking_enabled,
            total_duration=job.total_duration,
            chunk_duration=job.chunk_duration,
            total_chunks=len(chunks),
            chunks_completed=completed,
            chunks=chunks,
            processing_stats=job.processing_stats or {},
        )


class JobResultResponse(JobStatusResponse):
    download_url: Optional[str] = None
    preview_url: Optional[str] = None
    available_stems: List[str] = Field(default_factory=list)

    @classmethod
    def from_job(cls, job: Job) -> "JobResultResponse":  # type: ignore[override]
        base = JobStatusResponse.from_job(job).model_dump()
        stems = sorted(name for name, path in job.stems_files.items() if path.exists())
        preview = None
        if stems:
            preferred = "vocals" if "vocals" in stems else stems[0]
            preview = f"/jobs/{job.job_id}/stems/{preferred}"
        return cls(**base, download_url=None, preview_url=preview, available_stems=stems)
