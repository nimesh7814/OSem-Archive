from __future__ import annotations

import asyncio
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse

from .boxes_export import query_boxes_aggregated, to_csv, to_geojson
from .job_store import EXPORT_DIR, JobStatus, create_job, delete_job, get_job, list_jobs


def _sync_query_and_write(params: dict, file_type: str, file_path: str) -> int:
    """Runs in a worker thread: executes the DB query and writes the result file."""
    loop = asyncio.new_event_loop()
    try:
        rows: list[dict] = loop.run_until_complete(query_boxes_aggregated(**params))
    finally:
        loop.close()

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    content = to_csv(rows) if file_type == "csv" else to_geojson(rows)
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return len(rows)


async def _run_export(job_id: str) -> None:
    job = get_job(job_id)
    if job is None:
        return
    job.status = JobStatus.RUNNING
    file_path = os.path.join(EXPORT_DIR, f"{job_id}.{job.file_type}")
    try:
        row_count = await asyncio.to_thread(
            _sync_query_and_write, job.params, job.file_type, file_path
        )
        job.file_path = file_path
        job.row_count = row_count
        job.status = JobStatus.DONE
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error = str(exc)


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
    if aggregate not in ("raw", "date", "month", "year"):
        raise HTTPException(400, "aggregate must be one of: raw, date, month, year")
    if to_date and not from_date:
        raise HTTPException(400, "from_date is required when to_date is provided")

    params = dict(
        geometry_wkt=geometry_wkt,
        box_id=box_id,
        country=country,
        region=region,
        exposure=exposure,
        phenomenon=phenomenon,
        sensor_type=sensor_type,
        from_date=from_date,
        to_date=to_date,
        aggregate=aggregate,
    )
    job = create_job(params, file_type)
    asyncio.create_task(_run_export(job.job_id))
    return job.to_dict()


async def list_export_jobs() -> list[dict]:
    return [j.to_dict() for j in list_jobs()]


async def get_export_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return job.to_dict()


async def download_export(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job '{job_id}' not found")
    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        raise HTTPException(202, f"Job is still {job.status}; try again later")
    if job.status == JobStatus.FAILED:
        raise HTTPException(500, f"Job failed: {job.error}")
    if not job.file_path or not os.path.exists(job.file_path):
        raise HTTPException(404, "Export file not found on disk")

    media_type = "text/csv" if job.file_type == "csv" else "application/geo+json"
    return FileResponse(
        path=job.file_path,
        media_type=media_type,
        filename=f"export_{job_id}.{job.file_type}",
    )


async def delete_export_job(job_id: str) -> None:
    if not delete_job(job_id):
        raise HTTPException(404, f"Job '{job_id}' not found")
