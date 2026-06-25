"""
api/functions/job_store.py

In-memory export job registry. Jobs are lost on process restart — acceptable
for this study project. A Redis-backed store would be needed for production.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict

EXPORT_DIR: str = os.getenv("EXPORT_DIR", "/tmp/osem_exports")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ExportJob:
    job_id: str
    status: JobStatus
    created_at: datetime
    params: dict
    file_type: str
    file_path: str | None = None
    row_count: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "file_type": self.file_type,
            "row_count": self.row_count,
            "error": self.error,
        }


_jobs: Dict[str, ExportJob] = {}


def create_job(params: dict, file_type: str) -> ExportJob:
    job_id = str(uuid.uuid4())
    job = ExportJob(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime.utcnow(),
        params=params,
        file_type=file_type,
    )
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> ExportJob | None:
    return _jobs.get(job_id)


def list_jobs() -> list[ExportJob]:
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


def delete_job(job_id: str) -> bool:
    job = _jobs.pop(job_id, None)
    if job is None:
        return False
    if job.file_path and os.path.exists(job.file_path):
        try:
            os.remove(job.file_path)
        except OSError:
            pass
    return True
