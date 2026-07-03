import csv
import json
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg2.extras
from celery import Celery
from fastapi import HTTPException
from fastapi.responses import FileResponse

from connection import execute_query
from db_query import apply_filter, get_box_ids_by_aoi, get_box_ids_by_region

base_dir = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.getenv("EXPORT_DIR", os.path.join(base_dir, "exports"))
os.makedirs(EXPORT_DIR, exist_ok=True)

# Safety valve for "raw" exports over a wide date range.
EXPORT_MAX_ROWS = int(os.getenv("EXPORT_MAX_ROWS", "5000000"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

celery_app = Celery("osem_export", broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    result_expires=86400,
)

# aggregate -> source table. "raw" reads measurements; the rest read continuous aggregates.
AGGREGATE_TABLES = {
    "raw": "measurements",
    "hourly": "reading_hourly",
    "daily": "reading_daily",
    "monthly": "reading_monthly",
    "yearly": "reading_yearly",
}

# raw/hourly are split one file per month and zipped; daily/monthly/yearly stay a single file.
MONTHLY_SPLIT_AGGREGATES = {"raw", "hourly"}

# Single source of truth for export_job's column order.
JOB_COLUMNS = [
    "id", "format", "aggregate", "filters", "status",
    "row_count", "file_url", "error_message", "created_at", "completed_at",
]


# json.dump's default callback must raise on anything it can't handle; _csv_value below doesn't need to.
def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _csv_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


# Safe to drop into a Content-Disposition filename on any OS/filesystem.
def _safe_filename_part(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_") or "export"


def _row_to_job(row):
    return dict(zip(JOB_COLUMNS, row))


def _fetch_job(job_id):
    rows = execute_query(
        f"SELECT {', '.join(JOB_COLUMNS)} FROM export_job WHERE id = %s",
        (job_id,),
    )
    return _row_to_job(rows[0]) if rows else None


def _mark_running(job_id):
    execute_query("UPDATE export_job SET status = 'running' WHERE id = %s", (job_id,))


def _mark_done(job_id, row_count, file_path):
    execute_query(
        """
        UPDATE export_job
        SET status = 'done', row_count = %s, file_url = %s, completed_at = %s
        WHERE id = %s
        """,
        (row_count, file_path, datetime.now(timezone.utc), job_id),
    )


def _mark_failed(job_id, error_message):
    execute_query(
        """
        UPDATE export_job
        SET status = 'failed', error_message = %s, completed_at = %s
        WHERE id = %s
        """,
        (error_message, datetime.now(timezone.utc), job_id),
    )


# Builds the SELECT for one aggregate tier with a date range and box/exposure/tags/phenomenon filters.
def build_filtered_query(aggregate, filters):
    table = AGGREGATE_TABLES[aggregate]
    alias = "m" if aggregate == "raw" else "r"
    time_column = f"{alias}.time" if aggregate == "raw" else f"{alias}.bucket"
    value_columns = (
        f"{alias}.time, {alias}.value" if aggregate == "raw"
        else f"{alias}.bucket, {alias}.sum_value, {alias}.rdgs_count, {alias}.avg_value, {alias}.min_value, {alias}.max_value"
    )

    query = f'''
        SELECT
            b.id, b.name, b.box_type, b.exposure, b.model,
            ST_Y(b.location) AS latitude, ST_X(b.location) AS longitude,
            s.id, s.title, s.unit, s.sensor_type,
            {value_columns}
        FROM {table} {alias}
        JOIN sensors s ON s.id = {alias}.sensor_id
        JOIN boxes b ON b.id = s.box_id
        WHERE TRUE
    '''
    parameters = []

    if filters.get("from_date"):
        query += f" AND {time_column} >= %s"
        parameters.append(filters["from_date"])

    if filters.get("to_date"):
        query += f" AND {time_column} <= %s"
        parameters.append(filters["to_date"])

    query, parameters = apply_filter(query, parameters, "b.id", filters.get("box_ids"))
    query, parameters = apply_filter(query, parameters, "b.exposure", filters.get("exposure"))
    query, parameters = apply_filter(query, parameters, "s.sensor_type", filters.get("tags"))
    query, parameters = apply_filter(query, parameters, "s.title", filters.get("phenomenon"))

    query += f" ORDER BY {time_column} LIMIT %s"
    parameters.append(EXPORT_MAX_ROWS)

    return query, parameters


# Groups build_filtered_query's flat rows into box -> sensors -> measurements.
def group_measurement_rows(rows, aggregate):
    boxes = {}

    for row in rows:
        (
            box_id, name, box_type, exposure, model,
            latitude, longitude,
            sensor_id, title, unit, sensor_type,
            *values,
        ) = row

        if box_id not in boxes:
            boxes[box_id] = {
                "_id": box_id,
                "name": name,
                "boxType": box_type,
                "exposure": exposure,
                "model": model,
                "currentLocation": {
                    "coordinates": [longitude, latitude],
                    "type": "Point",
                },
                "sensors": {},
            }

        sensors_by_id = boxes[box_id]["sensors"]
        if sensor_id not in sensors_by_id:
            sensors_by_id[sensor_id] = {
                "_id": sensor_id,
                "boxes_id": box_id,
                "title": title,
                "unit": unit,
                "sensorType": sensor_type,
                "measurements": [],
            }

        if aggregate == "raw":
            time, value = values
            measurement = {"createdAt": time, "value": value}
        else:
            bucket, sum_value, rdgs_count, avg_value, min_value, max_value = values
            measurement = {
                "bucket": bucket,
                "sum_value": sum_value,
                "rdgs_count": rdgs_count,
                "avg_value": avg_value,
                "min_value": min_value,
                "max_value": max_value,
            }

        sensors_by_id[sensor_id]["measurements"].append(measurement)

    for box in boxes.values():
        box["sensors"] = list(box["sensors"].values())

    return list(boxes.values())


def write_csv(boxes, file_path, aggregate):
    base_columns = [
        "box_id", "box_name", "longitude", "latitude", "exposure", "boxType", "model",
        "sensor_id", "sensorType", "sensor_title", "sensor_unit",
    ]
    measurement_columns = (
        ["time", "value"] if aggregate == "raw"
        else ["bucket", "sum_value", "rdgs_count", "avg_value", "min_value", "max_value"]
    )
    columns = base_columns + measurement_columns

    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)

        for box in boxes:
            longitude, latitude = box["currentLocation"]["coordinates"]
            box_values = [
                box["_id"], box["name"], longitude, latitude,
                box["exposure"], box["boxType"], box["model"],
            ]

            for sensor in box["sensors"]:
                sensor_values = [sensor["_id"], sensor["sensorType"], sensor["title"], sensor["unit"]]

                for measurement in sensor["measurements"]:
                    if aggregate == "raw":
                        measurement_values = [_csv_value(measurement["createdAt"]), measurement["value"]]
                    else:
                        measurement_values = [
                            _csv_value(measurement["bucket"]),
                            measurement["sum_value"],
                            measurement["rdgs_count"],
                            measurement["avg_value"],
                            measurement["min_value"],
                            measurement["max_value"],
                        ]

                    writer.writerow(box_values + sensor_values + measurement_values)


def write_geojson(boxes, file_path):
    features = []
    for box in boxes:
        longitude, latitude = box["currentLocation"]["coordinates"]
        properties = {k: v for k, v in box.items() if k != "currentLocation"}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
            "properties": properties,
        })

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(
            {"type": "FeatureCollection", "features": features},
            f,
            default=_json_default,
            ensure_ascii=False,
        )


# row[11] is always the tier's primary timestamp column (time for raw, bucket otherwise).
def _row_month_key(row):
    return row[11].strftime("%Y-%m")


# One file per calendar month, zipped together.
def write_monthly_zip(rows, aggregate, file_format, job_id):
    rows_by_month = {}
    for row in rows:
        rows_by_month.setdefault(_row_month_key(row), []).append(row)

    zip_path = os.path.join(EXPORT_DIR, f"{job_id}.zip")

    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for month_key in sorted(rows_by_month):
                boxes = group_measurement_rows(rows_by_month[month_key], aggregate)
                file_name = f"{month_key}.{file_format}"
                file_path = os.path.join(tmp_dir, file_name)

                if file_format == "csv":
                    write_csv(boxes, file_path, aggregate)
                else:
                    write_geojson(boxes, file_path)

                zf.write(file_path, arcname=file_name)

    return zip_path


@celery_app.task(bind=True)
def run_export_job(self, job_id):
    """Celery task: runs the query, writes the export file, updates export_job."""
    job = _fetch_job(job_id)
    if job is None:
        return {"status": "failed", "error": "export_job not found"}

    _mark_running(job_id)

    try:
        sql, params = build_filtered_query(job["aggregate"], job["filters"] or {})
        rows = execute_query(sql, tuple(params))

        if job["aggregate"] in MONTHLY_SPLIT_AGGREGATES:
            file_path = write_monthly_zip(rows, job["aggregate"], job["format"], job_id)
        else:
            boxes = group_measurement_rows(rows, job["aggregate"])
            file_path = os.path.join(EXPORT_DIR, f"{job_id}.{job['format']}")

            if job["format"] == "csv":
                write_csv(boxes, file_path, job["aggregate"])
            else:
                write_geojson(boxes, file_path)

        _mark_done(job_id, len(rows), file_path)
        return {"status": "done", "row_count": len(rows), "file_url": file_path}

    except Exception as exc:
        _mark_failed(job_id, str(exc))
        raise


def create_export_job(file_format, aggregate, filters):
    if aggregate not in AGGREGATE_TABLES:
        raise HTTPException(status_code=400, detail=f"aggregate must be one of {list(AGGREGATE_TABLES)}")

    if file_format not in ("csv", "geojson"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'geojson'")

    row = execute_query(
        f"""
        INSERT INTO export_job (format, aggregate, filters, status)
        VALUES (%s, %s, %s, 'pending')
        RETURNING {', '.join(JOB_COLUMNS)}
        """,
        (file_format, aggregate, psycopg2.extras.Json(filters)),
    )[0]

    job = _row_to_job(row)
    async_result = run_export_job.delay(str(job["id"]))

    return {"job_id": str(job["id"]), "status": job["status"], "task_id": async_result.id}


def create_region_export(region, from_date, to_date, aggregate, file_format, exposure="all", tags="all", phenomenon="all"):
    box_ids, geometry_json = get_box_ids_by_region(region)
    if geometry_json is None:
        raise HTTPException(status_code=404, detail=f"Region '{region}' not found")

    filters = {
        "source_name": region,
        "region": region,
        "box_ids": box_ids,
        "exposure": exposure,
        "tags": tags,
        "phenomenon": phenomenon,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
    }
    return create_export_job(file_format, aggregate, filters)


def create_aoi_export(geometry_filename, from_date, to_date, aggregate, file_format, exposure="all", tags="all", phenomenon="all", source_name=None):
    try:
        box_ids, _ = get_box_ids_by_aoi(geometry_filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    filters = {
        "source_name": source_name or os.path.splitext(os.path.basename(geometry_filename))[0],
        "region": None,
        "box_ids": box_ids,
        "exposure": exposure,
        "tags": tags,
        "phenomenon": phenomenon,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
    }
    return create_export_job(file_format, aggregate, filters)


def list_export_jobs():
    rows = execute_query(f"SELECT {', '.join(JOB_COLUMNS)} FROM export_job ORDER BY created_at DESC")
    return [_row_to_job(row) for row in rows or []]


def get_export_job(job_id):
    return _fetch_job(job_id)


def download_export_job(job_id):
    job = _fetch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job '{job_id}' not found")

    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Export job is '{job['status']}', not ready yet")

    if not job["file_url"] or not os.path.exists(job["file_url"]):
        raise HTTPException(status_code=410, detail="Export file is missing or has expired")

    zipped = job["aggregate"] in MONTHLY_SPLIT_AGGREGATES
    media_type = "application/zip" if zipped else ("text/csv" if job["format"] == "csv" else "application/geo+json")

    source_name = _safe_filename_part((job["filters"] or {}).get("source_name"))
    timestamp = (job["completed_at"] or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    extension = "zip" if zipped else job["format"]

    return FileResponse(
        path=job["file_url"],
        media_type=media_type,
        filename=f"{source_name}_{timestamp}.{extension}",
    )


def delete_export_job(job_id):
    job = _fetch_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job '{job_id}' not found")

    execute_query("DELETE FROM export_job WHERE id = %s", (job_id,))

    if job["file_url"] and os.path.exists(job["file_url"]):
        os.remove(job["file_url"])
