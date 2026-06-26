from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum

from celery import Celery
from fastapi import HTTPException
from fastapi.responses import FileResponse
from psycopg2.extras import Json

from .boxes import VALID_AGGREGATES, query_boxes_aggregated, to_csv, to_geojson
from .db_con import get_db_connection

EXPORT_DIR = os.getenv("EXPORT_DIR", "/tmp/osem_exports")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)

celery_app = Celery(
    "osem_exports",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Berlin",
    enable_utc=True,
)

if os.getenv("CELERY_TASK_ALWAYS_EAGER", "").lower() in {"1", "true", "yes"}:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _ensure_jobs_table() -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS export_jobs (
                    job_id UUID PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    params JSONB NOT NULL,
                    file_type TEXT NOT NULL,
                    file_path TEXT,
                    row_count INTEGER,
                    error TEXT
                )
                """
            )


def _row_to_dict(row) -> dict:
    return {
        "job_id": str(row[0]),
        "status": row[1],
        "created_at": row[2].isoformat() if isinstance(row[2], datetime) else row[2],
        "file_type": row[3],
        "row_count": row[4],
        "error": row[5],
    }


def create_job(params: dict, file_type: str) -> dict:
    _ensure_jobs_table()
    job_id = str(uuid.uuid4())
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO export_jobs (job_id, status, params, file_type)
                VALUES (%s, %s, %s, %s)
                RETURNING job_id, status, created_at, file_type, row_count, error
                """,
                (job_id, JobStatus.PENDING.value, Json(params), file_type),
            )
            return _row_to_dict(cur.fetchone())


def get_job(job_id: str) -> dict | None:
    _ensure_jobs_table()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, status, created_at, file_type, row_count, error
                FROM export_jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_job_for_worker(job_id: str) -> dict | None:
    _ensure_jobs_table()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, status, created_at, params, file_type, file_path, row_count, error
                FROM export_jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "job_id": str(row[0]),
        "status": row[1],
        "created_at": row[2],
        "params": row[3],
        "file_type": row[4],
        "file_path": row[5],
        "row_count": row[6],
        "error": row[7],
    }


def list_jobs() -> list[dict]:
    _ensure_jobs_table()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, status, created_at, file_type, row_count, error
                FROM export_jobs
                ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall()
    return [_row_to_dict(row) for row in rows]


def update_job(
    job_id: str,
    status: JobStatus,
    file_path: str | None = None,
    row_count: int | None = None,
    error: str | None = None,
) -> None:
    _ensure_jobs_table()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE export_jobs
                SET status = %s,
                    file_path = COALESCE(%s, file_path),
                    row_count = COALESCE(%s, row_count),
                    error = %s
                WHERE job_id = %s
                """,
                (status.value, file_path, row_count, error, job_id),
            )


def delete_job(job_id: str) -> bool:
    _ensure_jobs_table()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT file_path FROM export_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            if row is None:
                return False

            file_path = row[0]
            cur.execute("DELETE FROM export_jobs WHERE job_id = %s", (job_id,))

    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass
    return True


def _query_rows(params: dict) -> list[dict]:
    return asyncio.run(query_boxes_aggregated(**params))


def write_export_file(params: dict, file_type: str, file_path: str) -> int:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        rows = _query_rows(params)
    else:
        with ThreadPoolExecutor(max_workers=1) as executor:
            rows = executor.submit(_query_rows, params).result()

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    content = to_csv(rows) if file_type == "csv" else to_geojson(rows)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return len(rows)


@celery_app.task(name="exports.run_export")
def run_export_task(job_id: str) -> None:
    job = get_job_for_worker(job_id)
    if job is None:
        return

    file_path = os.path.join(EXPORT_DIR, f"{job_id}.{job['file_type']}")
    update_job(job_id, JobStatus.RUNNING)

    try:
        row_count = write_export_file(job["params"], job["file_type"], file_path)
    except Exception as exc:
        update_job(job_id, JobStatus.FAILED, error=str(exc))
        raise

    update_job(job_id, JobStatus.DONE, file_path=file_path, row_count=row_count)


async def create_export(
    file_type: str,
    country: str | None,
    region: str | None,
    exposure: str | None,
    phenomenon: str | None,
    sensor_type: str | None,
    from_date: str | None,
    to_date: str | None,
    aggregate: str,
    box_id: str | None,
    geometry_wkt: str | None,
) -> dict:
    if file_type not in ("geojson", "csv"):
        raise HTTPException(400, "file_type must be 'geojson' or 'csv'")
    if aggregate not in VALID_AGGREGATES:
        raise HTTPException(
            400,
            f"aggregate must be one of: {', '.join(sorted(VALID_AGGREGATES))}",
        )
    if to_date and not from_date:
        raise HTTPException(400, "from_date is required when to_date is provided")

    params = {
        "geometry_wkt": geometry_wkt,
        "box_id": box_id,
        "country": country,
        "region": region,
        "exposure": exposure,
        "phenomenon": phenomenon,
        "sensor_type": sensor_type,
        "from_date": from_date,
        "to_date": to_date,
        "aggregate": aggregate,
    }
    job = create_job(params, file_type)
    try:
        run_export_task.delay(job["job_id"])
    except Exception as exc:
        update_job(job["job_id"], JobStatus.FAILED, error=str(exc))
        raise HTTPException(503, f"Could not enqueue export job: {exc}") from exc
    return job


async def list_export_jobs() -> list[dict]:
    return list_jobs()


async def get_export_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return job


async def download_export(job_id: str) -> FileResponse:
    job = get_job_for_worker(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    if job["status"] in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
        raise HTTPException(202, f"Job is still {job['status']}; try again later")
    if job["status"] == JobStatus.FAILED.value:
        raise HTTPException(500, f"Job failed: {job['error']}")
    if not job["file_path"] or not os.path.exists(job["file_path"]):
        raise HTTPException(404, "Export file not found on disk")

    media_type = "text/csv" if job["file_type"] == "csv" else "application/geo+json"
    return FileResponse(
        path=job["file_path"],
        media_type=media_type,
        filename=f"export_{job_id}.{job['file_type']}",
    )


async def delete_export_job(job_id: str) -> None:
    if not delete_job(job_id):
        raise HTTPException(404, f"Job '{job_id}' not found")
