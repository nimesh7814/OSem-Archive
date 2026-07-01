import csv
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from api.functions.celery_config import celery_app
from api.functions.config import EXPORT_DIR, EXPORT_JOB_CACHE_TTL
from api.functions.db_con import get_db_connection
from api.functions.redis_cache import (
    cached_or_compute,
    get_cached_json,
    redis_client,
    set_cached_json,
)
from api.functions.upload_aoi import load_geometry_from_upload_or_geometry


AGGREGATE_TABLES = {
    "raw": "measurements",
    "hourly": "reading_hourly",
    "daily": "reading_daily",
    "monthly": "reading_monthly",
    "yearly": "reading_yearly",
}

EXPORT_JOBS_CACHE_KEY = "exports:jobs"


def split_csv_value(value: Optional[str]):
    if not value:
        return None

    values = [part.strip() for part in value.split(",") if part.strip()]
    return values or None


def parse_optional_date(value: Optional[str], field_name: str) -> Optional[str]:
    """Validate an ISO 8601 date/time string before it reaches the SQL layer."""
    if not value:
        return None

    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field_name} must be a valid ISO 8601 date/time.")

    return value


def normalize_file_format(file_format: Optional[str], file_type: Optional[str] = None) -> str:
    selected_format = file_format or file_type or "geojson"
    if selected_format == "json":
        selected_format = "geojson"
    return selected_format


def export_json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def export_csv_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


class ExportFilters:
    def __init__(
        self,
        country: Optional[str],
        exposure: Optional[str],
        phenomenon: Optional[str],
        sensor_type: Optional[str],
        box_id: Optional[str],
        from_date: Optional[str],
        to_date: Optional[str],
    ):
        self.country = country
        self.exposure = exposure
        self.phenomenon = split_csv_value(phenomenon)
        self.sensor_type = split_csv_value(sensor_type)
        self.box_ids = [b.strip() for b in box_id.split(",") if b.strip()] if box_id else None
        self.from_date = parse_optional_date(from_date, "from_date")
        self.to_date = parse_optional_date(to_date, "to_date")


def export_filters_query(
    country: Optional[str] = Query(None),
    exposure: Optional[str] = Query(None),
    phenomenon: Optional[str] = Query(None),
    sensor_type: Optional[str] = Query(None),
    box_id: Optional[str] = Query(None, description="Comma-separated box IDs"),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
) -> ExportFilters:
    return ExportFilters(country, exposure, phenomenon, sensor_type, box_id, from_date, to_date)


def export_filters_form(
    country: Optional[str] = Form(None),
    exposure: Optional[str] = Form(None),
    phenomenon: Optional[str] = Form(None),
    sensor_type: Optional[str] = Form(None),
    box_id: Optional[str] = Form(None, description="Comma-separated box IDs"),
    from_date: Optional[str] = Form(None),
    to_date: Optional[str] = Form(None),
) -> ExportFilters:
    return ExportFilters(country, exposure, phenomenon, sensor_type, box_id, from_date, to_date)


def export_job_cache_key(job_id: str) -> str:
    return f"exports:job:{job_id}"


def cache_export_job(job):
    if job is None:
        return None

    set_cached_json(export_job_cache_key(str(job["id"])), job, ttl=EXPORT_JOB_CACHE_TTL)
    redis_client.delete(EXPORT_JOBS_CACHE_KEY)
    return job


def clear_export_job_cache(job_id: str):
    redis_client.delete(export_job_cache_key(str(job_id)))
    redis_client.delete(EXPORT_JOBS_CACHE_KEY)


def fetch_export_job_from_db(job_id: str):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SELECT * FROM export_job WHERE id = %s", (job_id,))
            return cursor.fetchone()


def fetch_export_job(job_id: str):
    cached = get_cached_json(export_job_cache_key(str(job_id)))
    if cached is not None:
        return cached

    return cache_export_job(fetch_export_job_from_db(job_id))


def create_export_job(file_format: str, aggregate: str, filters: dict):
    file_format = normalize_file_format(file_format)

    if file_format not in ("csv", "geojson"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'geojson'")

    if aggregate not in AGGREGATE_TABLES:
        raise HTTPException(status_code=400, detail=f"aggregate must be one of {list(AGGREGATE_TABLES)}")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """
                INSERT INTO export_job (format, aggregate, filters, status)
                VALUES (%s, %s, %s, 'pending')
                RETURNING *
                """,
                (file_format, aggregate, psycopg2.extras.Json(filters)),
            )
            job = cursor.fetchone()
        conn.commit()

    cache_export_job(job)
    async_result = export_measurements.delay(str(job["id"]))

    return {"job_id": str(job["id"]), "status": job["status"], "task_id": async_result.id}


def build_filtered_query(aggregate: str, filters: dict):
    source_table = AGGREGATE_TABLES[aggregate]
    time_column = "m.time" if aggregate == "raw" else "r.bucket"

    conditions = []
    params = []

    if filters.get("from_date"):
        conditions.append(f"{time_column} >= %s")
        params.append(filters["from_date"])

    if filters.get("to_date"):
        conditions.append(f"{time_column} <= %s")
        params.append(filters["to_date"])

    if filters.get("country"):
        conditions.append("rg.country = %s")
        params.append(filters["country"])

    if filters.get("region"):
        conditions.append("rg.region = %s")
        params.append(filters["region"])

    if filters.get("exposure"):
        conditions.append("b.exposure = %s")
        params.append(filters["exposure"])

    # Frontend versions have used both title and sensor_type names for this filter.
    if filters.get("phenomenon"):
        conditions.append("(s.sensor_type = ANY(%s) OR s.title = ANY(%s))")
        params.append(filters["phenomenon"])
        params.append(filters["phenomenon"])

    if filters.get("sensor_type"):
        conditions.append("(s.title = ANY(%s) OR s.sensor_type = ANY(%s))")
        params.append(filters["sensor_type"])
        params.append(filters["sensor_type"])

    if filters.get("box_ids"):
        conditions.append("b.id = ANY(%s)")
        params.append(filters["box_ids"])

    if filters.get("area_wkt"):
        conditions.append("ST_Within(b.location, ST_GeomFromText(%s, 4326))")
        params.append(filters["area_wkt"])

    where_clause = (" AND " + " AND ".join(conditions)) if conditions else ""

    if aggregate == "raw":
        sql = f"""
            SELECT
                b.id AS box_id,
                b.name AS box_name,
                b.box_type,
                b.exposure,
                b.model,
                b.created_at AS box_created_at,
                b.updated_at AS box_updated_at,
                ST_X(b.location) AS longitude,
                ST_Y(b.location) AS latitude,
                rg.country,
                rg.region,
                s.id AS sensor_id,
                s.sensor_type,
                s.title,
                s.unit,
                m.time,
                m.value
            FROM measurements m
            JOIN sensors s ON s.id = m.sensor_id
            JOIN boxes b ON b.id = s.box_id
            JOIN regions rg ON rg.id = b.region_id
            WHERE TRUE{where_clause}
            ORDER BY m.time
        """
    else:
        sql = f"""
            SELECT
                b.id AS box_id,
                b.name AS box_name,
                b.box_type,
                b.exposure,
                b.model,
                b.created_at AS box_created_at,
                b.updated_at AS box_updated_at,
                ST_X(b.location) AS longitude,
                ST_Y(b.location) AS latitude,
                rg.country,
                rg.region,
                s.id AS sensor_id,
                s.sensor_type,
                s.title,
                s.unit,
                r.bucket,
                r.sum_value,
                r.rdgs_count,
                r.avg_value,
                r.min_value,
                r.max_value
            FROM {source_table} r
            JOIN sensors s ON s.id = r.sensor_id
            JOIN boxes b ON b.id = s.box_id
            JOIN regions rg ON rg.id = b.region_id
            WHERE TRUE{where_clause}
            ORDER BY r.bucket
        """

    return sql, params


def measurement_from_row(row, aggregate: str):
    if aggregate == "raw":
        return {
            "createdAt": row["time"],
            "value": row["value"],
        }

    return {
        "bucket": row["bucket"],
        "sum_value": row["sum_value"],
        "rdgs_count": row["rdgs_count"],
        "avg_value": row["avg_value"],
        "min_value": row["min_value"],
        "max_value": row["max_value"],
    }


def measurement_timestamp(measurement: dict, aggregate: str):
    return measurement["createdAt"] if aggregate == "raw" else measurement["bucket"]


def group_rows_by_box(rows, aggregate: str):
    boxes = {}

    for row in rows:
        box_id = row["box_id"]
        if box_id not in boxes:
            boxes[box_id] = {
                "_id": box_id,
                "name": row["box_name"],
                "sensors": [],
                "exposure": row["exposure"],
                "createdAt": row["box_created_at"],
                "model": row["model"],
                "currentLocation": {
                    "coordinates": [row["longitude"], row["latitude"]],
                    "type": "Point",
                    "timestamp": row["box_updated_at"],
                },
                "lastMeasurementAt": None,
                "updatedAt": row["box_updated_at"],
                "boxType": row["box_type"],
                "country": row["country"],
                "region": row["region"],
                "_sensor_index": {},
            }

        box = boxes[box_id]
        sensor_id = row["sensor_id"]
        sensors_by_id = box["_sensor_index"]

        if sensor_id not in sensors_by_id:
            sensor = {
                "_id": sensor_id,
                "__v": 0,
                "boxes_id": box_id,
                "lastMeasurement": None,
                "sensorType": row["sensor_type"],
                "title": row["title"],
                "unit": row["unit"],
                "measurements": [],
            }
            sensors_by_id[sensor_id] = sensor
            box["sensors"].append(sensor)

        sensor = sensors_by_id[sensor_id]
        measurement = measurement_from_row(row, aggregate)
        sensor["measurements"].append(measurement)

        current_time = measurement_timestamp(measurement, aggregate)
        if box["lastMeasurementAt"] is None or current_time > box["lastMeasurementAt"]:
            box["lastMeasurementAt"] = current_time

        last_measurement = sensor["lastMeasurement"]
        if last_measurement is None or current_time > last_measurement["createdAt"]:
            sensor["lastMeasurement"] = {
                "createdAt": current_time,
                "value": row["value"] if aggregate == "raw" else row["avg_value"],
            }

    for box in boxes.values():
        box.pop("_sensor_index", None)

    return list(boxes.values())


def write_csv(boxes, file_path, aggregate: str):
    base_columns = [
        "box_id", "box_name", "longitude", "latitude", "exposure",
        "box_createdAt", "box_model", "box_lastMeasurementAt",
        "box_updatedAt", "boxType", "country", "region",
        "sensor_id", "sensorType", "sensor_title", "sensor_unit",
        "sensor_lastMeasurementAt", "sensor_lastMeasurementValue",
    ]
    measurement_columns = (
        ["measurement_time", "measurement_value"]
        if aggregate == "raw"
        else ["bucket", "sum_value", "rdgs_count", "avg_value", "min_value", "max_value"]
    )
    columns = base_columns + measurement_columns

    with open(file_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for box in boxes:
            longitude, latitude = box["currentLocation"]["coordinates"]
            box_values = [
                box["_id"],
                box["name"],
                longitude,
                latitude,
                box["exposure"],
                export_csv_value(box["createdAt"]),
                box["model"],
                export_csv_value(box["lastMeasurementAt"]),
                export_csv_value(box["updatedAt"]),
                box["boxType"],
                box["country"],
                box["region"],
            ]

            for sensor in box["sensors"]:
                last_measurement = sensor["lastMeasurement"] or {}
                sensor_values = [
                    sensor["_id"],
                    sensor["sensorType"],
                    sensor["title"],
                    sensor["unit"],
                    export_csv_value(last_measurement.get("createdAt")),
                    last_measurement.get("value"),
                ]

                for measurement in sensor["measurements"]:
                    if aggregate == "raw":
                        measurement_values = [
                            export_csv_value(measurement["createdAt"]),
                            measurement["value"],
                        ]
                    else:
                        measurement_values = [
                            export_csv_value(measurement["bucket"]),
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
        properties = dict(box)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [longitude, latitude],
            },
            "properties": properties,
        })

    with open(file_path, "w") as f:
        json.dump(
            {"type": "FeatureCollection", "features": features},
            f,
            default=export_json_default,
            ensure_ascii=False,
        )


@celery_app.task(bind=True)
def export_measurements(self, job_id: str):
    """Run export generation away from the request thread."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute("SELECT * FROM export_job WHERE id = %s", (job_id,))
            job = cursor.fetchone()

            if job is None:
                return {"status": "failed", "error": "export_job not found"}

            cursor.execute(
                "UPDATE export_job SET status = 'running' WHERE id = %s RETURNING *",
                (job_id,),
            )
            job = cursor.fetchone()
        conn.commit()
        cache_export_job(job)

    try:
        filters = job["filters"] or {}
        sql, params = build_filtered_query(job["aggregate"], filters)

        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()

        boxes = group_rows_by_box(rows, job["aggregate"])
        file_name = f"{job_id}.{job['format']}"
        file_path = os.path.join(EXPORT_DIR, file_name)

        if job["format"] == "csv":
            write_csv(boxes, file_path, job["aggregate"])
        else:
            write_geojson(boxes, file_path)

        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    """
                    UPDATE export_job
                    SET status = 'done', row_count = %s, file_url = %s, completed_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (len(rows), file_path, datetime.now(timezone.utc), job_id),
                )
                job = cursor.fetchone()
            conn.commit()
            cache_export_job(job)

        return {"status": "done", "row_count": len(rows), "file_url": file_path}

    except Exception as exc:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    """
                    UPDATE export_job
                    SET status = 'failed', error_message = %s, completed_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (str(exc), datetime.now(timezone.utc), job_id),
                )
                job = cursor.fetchone()
            conn.commit()
            cache_export_job(job)
        raise


def create_region_export(region: str, filters: ExportFilters, aggregate: str, file_format: str):
    job_filters = {
        "country": filters.country,
        "region": region,
        "exposure": filters.exposure,
        "phenomenon": filters.phenomenon,
        "sensor_type": filters.sensor_type,
        "box_ids": filters.box_ids,
        "area_wkt": None,
        "from_date": filters.from_date,
        "to_date": filters.to_date,
    }
    return create_export_job(file_format, aggregate, job_filters)


def create_aoi_export(
    area: Optional[UploadFile],
    geometry: Optional[str],
    filters: ExportFilters,
    aggregate: str,
    file_format: str,
):
    area_wkt = load_geometry_from_upload_or_geometry(area, geometry)

    job_filters = {
        "country": filters.country,
        "region": None,
        "exposure": filters.exposure,
        "phenomenon": filters.phenomenon,
        "sensor_type": filters.sensor_type,
        "box_ids": filters.box_ids,
        "area_wkt": area_wkt,
        "from_date": filters.from_date,
        "to_date": filters.to_date,
    }
    return create_export_job(file_format, aggregate, job_filters)


def list_export_jobs():
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM export_job ORDER BY created_at DESC")
                return cursor.fetchall()

    return cached_or_compute(EXPORT_JOBS_CACHE_KEY, _compute, ttl=EXPORT_JOB_CACHE_TTL)


def get_export_job(job_id: str):
    job = fetch_export_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")

    return job


def download_export(job_id: str):
    job = fetch_export_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")

    if job["status"] != "done":
        raise HTTPException(status_code=409, detail=f"Export job is '{job['status']}', not ready yet")

    if not job["file_url"] or not os.path.exists(job["file_url"]):
        raise HTTPException(status_code=410, detail="Export file is missing or has expired")

    media_type = "text/csv" if job["format"] == "csv" else "application/geo+json"
    return FileResponse(
        path=job["file_url"],
        media_type=media_type,
        filename=os.path.basename(job["file_url"]),
    )


def delete_export_job(job_id: str):
    job = fetch_export_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM export_job WHERE id = %s", (job_id,))
        conn.commit()

    clear_export_job_cache(job_id)

    if job["file_url"] and os.path.exists(job["file_url"]):
        os.remove(job["file_url"])
