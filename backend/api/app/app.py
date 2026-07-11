import json
import logging
import time as time_module
from typing import Annotated
from datetime import datetime, time, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

try:
    from .aoi import AoiValidationError, validate_aoi_file
    from .bucket import BucketError, check_object_storage_connection, get_presigned_download_url
    from .cache import CacheUnavailableError, check_cache_connection, get_cached, set_cached, key_exists
    from .celery_app import (
        ensure_export_workers_available,
        export_workers_available,
        send_export_task,
        get_export_result,
        ExportQueueError,
    )
    from .db import run_query, DatabaseBusyError, DatabaseQueryError, DatabaseConnectionLostError, DatabaseInputError
    from .format import format_box, format_measurement_boxes
    from .queries import TAGS_QUERY, PHENOMENA_QUERY, EXPOSURE_QUERY, MEASUREMENT_AGGREGATES
    from .schema import Summary, Tag, Phenomenon, Exposure, CommonMeasurementFilters, ExportFormat, MeasurementAggregate
except ImportError:
    from aoi import AoiValidationError, validate_aoi_file
    from bucket import BucketError, check_object_storage_connection, get_presigned_download_url
    from cache import CacheUnavailableError, check_cache_connection, get_cached, set_cached, key_exists
    from celery_app import (
        ensure_export_workers_available,
        export_workers_available,
        send_export_task,
        get_export_result,
        ExportQueueError,
    )
    from db import run_query, DatabaseBusyError, DatabaseQueryError, DatabaseConnectionLostError, DatabaseInputError
    from format import format_box, format_measurement_boxes
    from queries import TAGS_QUERY, PHENOMENA_QUERY, EXPOSURE_QUERY, MEASUREMENT_AGGREGATES
    from schema import Summary, Tag, Phenomenon, Exposure, CommonMeasurementFilters, ExportFormat, MeasurementAggregate

app = FastAPI()
logger = logging.getLogger(__name__)
request_logger = logging.getLogger("api.requests")


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    started_at = time_module.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time_module.perf_counter() - started_at) * 1000
        request_logger.exception(
            "HTTP request failed method=%s path=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time_module.perf_counter() - started_at) * 1000
    request_logger.info(
        "HTTP request completed method=%s path=%s status_code=%s duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response

TAGS_CACHE_KEY = "tags:details"
PHENOMENA_CACHE_KEY = "phenomena:details"
EXPOSURE_CACHE_KEY = "exposure:details"
EXPORT_JOB_KEY_PREFIX = "export_job:"


def mark_export_job_submitted(job_id):
    # Celery's AsyncResult reports "PENDING" both for a real, not-yet-started
    # job and for a job ID that was never submitted at all. Recording every
    # job we actually create lets /exports/{job_id} tell those apart and
    # return 404 for the latter instead of "queued" forever.
    set_cached(f"{EXPORT_JOB_KEY_PREFIX}{job_id}", True)

def normalize_filter(values):
    if not values or "all" in values:
        return None

    normalized = []
    for value in values:
        normalized.extend(part.strip() for part in value.split(",") if part.strip())

    if not normalized or "all" in normalized:
        return None
    return normalized


def error_response(status_code: int, message: str, code: int | None = None, details=None, headers=None):
    error = {
        "code": code or status_code,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def service_status(name, check):
    try:
        check()
    except Exception as exc:
        return name, {
            "status": "unavailable",
            "message": str(exc),
        }
    return name, {"status": "ok"}


def ensure_export_dependencies_available():
    check_object_storage_connection()
    ensure_export_workers_available()


def format_validation_errors(errors):
    formatted_errors = []
    for error in errors:
        location = [
            str(part)
            for part in error.get("loc", [])
            if part not in ("query", "path", "body")
        ]
        message = error.get("msg", "Invalid value.")
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        formatted_errors.append({
            "field": ".".join(location) if location else "request",
            "message": message,
            "type": error.get("type", "value_error"),
        })
    return formatted_errors


def get_cached_list(cache_key, query, schema, field_name):
    cached_result = get_cached(cache_key)

    if cached_result is not None:
        return cached_result, "cache"

    results = run_query(query, schema=schema)
    values = [getattr(row, field_name) for row in results]
    set_cached(cache_key, values)
    return values, "database"


def validate_known_filter_values(filter_name, requested_values, available_values):
    if requested_values is None:
        return

    filter_labels = {
        "tags": "tag",
        "phenomena": "phenomenon",
        "exposure": "exposure",
    }
    filter_label = filter_labels.get(filter_name, filter_name)
    available_values_set = set(available_values)
    invalid_values = list(dict.fromkeys(
        value
        for value in requested_values
        if value not in available_values_set
    ))

    if not invalid_values:
        return

    raise HTTPException(
        status_code=400,
        detail={
            "message": f"Unknown {filter_label} filter value.",
            "invalidValues": invalid_values,
            "hint": f"Use GET /{filter_name} to see the accepted values, or pass {filter_name}=all to disable this filter.",
        },
    )


def measurement_filter_params(
    from_date: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    tags: Annotated[list[str] | None, Query()] = None,
    phenomena: Annotated[list[str] | None, Query()] = None,
    exposure: Annotated[list[str] | None, Query()] = None,
    aggregate: Annotated[MeasurementAggregate, Query()] = "daily",
):
    return build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate)


def build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate):
    try:
        return CommonMeasurementFilters(**{
            "from": from_date,
            "to": to_date,
            "tags": tags,
            "phenomena": phenomena,
            "exposure": exposure,
            "aggregate": aggregate,
        })
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


def validate_measurement_filters(filters):
    tag_filter = normalize_filter(filters.tags)
    phenomenon_filter = normalize_filter(filters.phenomena)
    exposure_filter = normalize_filter(filters.exposure)

    if tag_filter is not None:
        tag_values, _ = get_cached_list(TAGS_CACHE_KEY, TAGS_QUERY, Tag, "sensor_type")
        validate_known_filter_values("tags", tag_filter, tag_values)

    if phenomenon_filter is not None:
        phenomenon_values, _ = get_cached_list(PHENOMENA_CACHE_KEY, PHENOMENA_QUERY, Phenomenon, "title")
        validate_known_filter_values("phenomena", phenomenon_filter, phenomenon_values)

    if exposure_filter is not None:
        exposure_values, _ = get_cached_list(EXPOSURE_CACHE_KEY, EXPOSURE_QUERY, Exposure, "exposure")
        validate_known_filter_values("exposure", exposure_filter, exposure_values)

    return tag_filter, phenomenon_filter, exposure_filter


def get_validated_measurement_filters(country, region, filters):
    region_rows = run_query('''
        SELECT id
        FROM regions
        WHERE country = %s AND region = %s
        LIMIT 1
    ''', (country, region))

    if not region_rows:
        raise HTTPException(status_code=404, detail=f"Region '{region}' in '{country}' not found")

    return validate_measurement_filters(filters)


def serialize_measurement_filters(filters):
    return {
        "from": filters.from_date.isoformat() if filters.from_date is not None else None,
        "to": filters.to_date.isoformat() if filters.to_date is not None else None,
        "tags": filters.tags,
        "phenomena": filters.phenomena,
        "exposure": filters.exposure,
        "aggregate": filters.aggregate,
    }


def get_region_measurement_rows(country, region, filters):
    tag_filter, phenomenon_filter, exposure_filter = get_validated_measurement_filters(country, region, filters)
    aggregate = MEASUREMENT_AGGREGATES[filters.aggregate]
    measurement_table = aggregate["table"]
    time_column = aggregate["time_column"]
    value_columns = aggregate["value_columns"]

    query = f'''
        SELECT
            %s AS aggregate,
            b.id AS box_id,
            b.name,
            b.exposure,
            b.model,
            b.created_at,
            b.updated_at,
            b.last_measurement_at,
            r.country,
            r.region,
            ST_X(b.location) AS longitude,
            ST_Y(b.location) AS latitude,
            s.id AS sensor_id,
            s.box_id,
            s.sensor_type,
            s.title,
            s.unit,
            {value_columns}
        FROM {measurement_table} d
        JOIN sensors s ON s.id = d.sensor_id
        JOIN boxes b ON b.id = s.box_id
        JOIN regions r ON r.id = b.region_id
        WHERE r.country = %s
          AND r.region = %s
          AND s.last_measurement IS NOT NULL
    '''
    params = [filters.aggregate, country, region]

    if filters.from_date is not None and filters.to_date is not None:
        to_date_end = datetime.combine(filters.to_date, time.max)
        query += f'''
          AND d.{time_column} >= %s
          AND d.{time_column} <= %s
        '''
        params.extend([filters.from_date, to_date_end])

    if tag_filter is not None:
        query += " AND s.sensor_type = ANY(%s)"
        params.append(tag_filter)

    if phenomenon_filter is not None:
        query += " AND s.title = ANY(%s)"
        params.append(phenomenon_filter)

    if exposure_filter is not None:
        query += " AND b.exposure = ANY(%s)"
        params.append(exposure_filter)

    query += f" ORDER BY b.id, s.id, d.{time_column}"

    return run_query(query, tuple(params))


def get_aoi_measurement_rows(geometry, filters):
    tag_filter, phenomenon_filter, exposure_filter = validate_measurement_filters(filters)
    aggregate = MEASUREMENT_AGGREGATES[filters.aggregate]
    measurement_table = aggregate["table"]
    time_column = aggregate["time_column"]
    value_columns = aggregate["value_columns"]

    query = f'''
        SELECT
            %s AS aggregate,
            b.id AS box_id,
            b.name,
            b.exposure,
            b.model,
            b.created_at,
            b.updated_at,
            b.last_measurement_at,
            r.country,
            r.region,
            ST_X(b.location) AS longitude,
            ST_Y(b.location) AS latitude,
            s.id AS sensor_id,
            s.box_id,
            s.sensor_type,
            s.title,
            s.unit,
            {value_columns}
        FROM {measurement_table} d
        JOIN sensors s ON s.id = d.sensor_id
        JOIN boxes b ON b.id = s.box_id
        LEFT JOIN regions r ON r.id = b.region_id
        WHERE s.last_measurement IS NOT NULL
          AND b.location IS NOT NULL
          AND ST_Intersects(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), b.location)
    '''
    params = [filters.aggregate, json.dumps(geometry)]

    if filters.from_date is not None and filters.to_date is not None:
        to_date_end = datetime.combine(filters.to_date, time.max)
        query += f'''
          AND d.{time_column} >= %s
          AND d.{time_column} <= %s
        '''
        params.extend([filters.from_date, to_date_end])

    if tag_filter is not None:
        query += " AND s.sensor_type = ANY(%s)"
        params.append(tag_filter)

    if phenomenon_filter is not None:
        query += " AND s.title = ANY(%s)"
        params.append(phenomenon_filter)

    if exposure_filter is not None:
        query += " AND b.exposure = ANY(%s)"
        params.append(exposure_filter)

    query += f" ORDER BY b.id, s.id, d.{time_column}"

    return run_query(query, tuple(params))


def get_aggregate_data_coverage(aggregate):
    cache_key = f"coverage:{aggregate}"
    cached_result = get_cached(cache_key)
    if cached_result is not None:
        return cached_result

    aggregate_config = MEASUREMENT_AGGREGATES[aggregate]
    rows = run_query(f'''
        SELECT
            min({aggregate_config["time_column"]}) AS min_time,
            max({aggregate_config["time_column"]}) AS max_time
        FROM {aggregate_config["table"]}
    ''')
    min_time = rows[0]["min_time"] if rows else None
    max_time = rows[0]["max_time"] if rows else None
    coverage = {
        "from": min_time.date().isoformat() if min_time else None,
        "to": max_time.date().isoformat() if max_time else None,
    }
    set_cached(cache_key, coverage)
    return coverage


def response_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_region_aoi(country, region):
    rows = run_query('''
        SELECT
            country,
            region,
            ST_AsGeoJSON(geometry)::json AS geometry,
            ST_Area(geometry::geography) / 1000000.0 AS area_sqkm
        FROM regions
        WHERE country = %s AND region = %s
        LIMIT 1
    ''', (country, region))

    if not rows:
        raise HTTPException(status_code=404, detail=f"Region '{region}' in '{country}' not found")

    row = rows[0]
    area_sqkm = row["area_sqkm"]
    return {
        "country": row["country"],
        "region": row["region"],
        "area_sqkm": round(float(area_sqkm), 6) if area_sqkm is not None else None,
        "geometry": row["geometry"],
    }


def build_region_measurement_payload(country, region, filters, rows):
    payload = {
        "input": "region",
        "time": response_timestamp(),
        "aoi": get_region_aoi(country, region),
        "aggregate": filters.aggregate,
        "from": filters.from_date.isoformat() if filters.from_date is not None else None,
        "to": filters.to_date.isoformat() if filters.to_date is not None else None,
        "exposure": filters.exposure,
        "boxes": format_measurement_boxes(rows),
        "source": "database",
    }

    # An empty result here looks identical to "this region has no sensors" —
    # but it's frequently just that the query fell outside the archive's
    # actual data coverage (see get_aggregate_data_coverage). Surface that
    # coverage window so an empty response isn't silently ambiguous.
    if not rows:
        coverage = get_aggregate_data_coverage(filters.aggregate)
        if coverage["from"] and coverage["to"]:
            payload["note"] = (
                f"No '{filters.aggregate}' measurements were found for {country}/{region} in this date range. "
                f"Archived '{filters.aggregate}' data is available from {coverage['from']} to {coverage['to']}."
            )

    return payload


def build_aoi_measurement_payload(aoi, filters, rows):
    payload = {
        "input": "aoi",
        "time": response_timestamp(),
        "aoi": {
            "name": aoi["properties"]["input"],
            "area_sqkm": aoi["properties"]["area_sqkm"],
            "geometry": aoi["geometry"],
        },
        "aggregate": filters.aggregate,
        "from": filters.from_date.isoformat() if filters.from_date is not None else None,
        "to": filters.to_date.isoformat() if filters.to_date is not None else None,
        "exposure": filters.exposure,
        "boxes": format_measurement_boxes(rows),
        "source": "database",
    }

    if not rows:
        coverage = get_aggregate_data_coverage(filters.aggregate)
        if coverage["from"] and coverage["to"]:
            payload["note"] = (
                f"No '{filters.aggregate}' measurements were found inside this AOI in this date range. "
                f"Archived '{filters.aggregate}' data is available from {coverage['from']} to {coverage['to']}."
            )

    return payload


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    details = None

    if exc.status_code == 404 and detail == "Not Found":
        message = f"No endpoint exists for {request.method} {request.url.path}."
        details = {
            "hint": "Use GET /countries to find available countries and regions, or GET /regions/{country}/{region}/measurements for measurements.",
            "availableEndpoints": [
                "GET /stats",
                "GET /boxes",
                "GET /countries",
                "GET /tags",
                "GET /phenomena",
                "GET /exposure",
                "POST /aoi/validate",
                "POST /aoi/measurements",
                "POST /aoi/measurements/exports",
                "GET /regions/{country}/{region}/measurements",
                "POST /regions/{country}/{region}/measurements/exports?format=csv&aggregate=daily",
                "POST /regions/{country}/{region}/measurements/exports?format=geojson&aggregate=daily",
                "GET /exports/{job_id}",
                "GET /exports/{job_id}/download",
            ],
        }
    elif isinstance(detail, dict):
        message = detail.get("message", "The request could not be completed.")
        details = {key: value for key, value in detail.items() if key != "message"}
        if not details:
            details = None
    else:
        message = str(detail)

    return error_response(exc.status_code, message, details=details, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    return error_response(
        status_code=422,
        message="One or more request parameters are invalid.",
        details=format_validation_errors(exc.errors()),
    )


@app.exception_handler(DatabaseBusyError)
async def database_busy_handler(request: Request, exc: DatabaseBusyError):
    return error_response(
        status_code=503,
        message="The database is busy. Please try again shortly.",
    )


@app.exception_handler(DatabaseConnectionLostError)
async def database_connection_lost_handler(request: Request, exc: DatabaseConnectionLostError):
    return error_response(
        status_code=503,
        message="The database connection was interrupted. Please try again.",
    )


@app.exception_handler(DatabaseQueryError)
async def database_query_error_handler(request: Request, exc: DatabaseQueryError):
    return error_response(
        status_code=500,
        message="The server could not complete the database query.",
    )


@app.exception_handler(DatabaseInputError)
async def database_input_error_handler(request: Request, exc: DatabaseInputError):
    return error_response(
        status_code=400,
        message="One or more request values contain characters that are not allowed.",
    )


@app.exception_handler(BucketError)
async def bucket_error_handler(request: Request, exc: BucketError):
    return error_response(
        status_code=503,
        message=str(exc),
    )


@app.exception_handler(CacheUnavailableError)
async def cache_unavailable_error_handler(request: Request, exc: CacheUnavailableError):
    return error_response(
        status_code=503,
        message=str(exc),
    )


@app.exception_handler(ExportQueueError)
async def export_queue_error_handler(request: Request, exc: ExportQueueError):
    return error_response(
        status_code=503,
        message=str(exc),
    )


@app.exception_handler(AoiValidationError)
async def aoi_validation_error_handler(request: Request, exc: AoiValidationError):
    return error_response(
        status_code=400,
        message=str(exc),
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    return error_response(
        status_code=500,
        message="An unexpected server error occurred.",
    )


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/health/dependencies")
def dependency_health_check():
    redis_name, redis_status = service_status("redis", check_cache_connection)
    minio_name, minio_status = service_status("minio", check_object_storage_connection)
    checks = {
        redis_name: redis_status,
        minio_name: minio_status,
    }

    if redis_status["status"] == "ok":
        celery_name, celery_status = service_status("celeryWorker", ensure_export_workers_available)
        checks[celery_name] = celery_status
    else:
        checks["celeryWorker"] = {
            "status": "unavailable",
            "message": "The export worker cannot be checked because Redis is unavailable.",
        }

    status_code = 200 if all(item["status"] == "ok" for item in checks.values()) else 503
    return JSONResponse(status_code=status_code, content={
        "status": "ok" if status_code == 200 else "degraded",
        "dependencies": checks,
    })


@app.get("/stats")
def get_stats():
    cache_key = "stats:summary"
    cached_result = get_cached(cache_key)

    if cached_result is not None:
        return {"summary": cached_result, "source": "cache"}

    results = run_query('''
        SELECT
            stations,
            sensors,
            readings,
            countries,
            updated_at
        FROM summary
    ''', schema=Summary)

    summary_results = [r.model_dump(mode="json") for r in results]
    set_cached(cache_key, summary_results)
    return {"summary": summary_results, "source": "database"}


@app.get("/boxes")
def get_boxes():
    cache_key = "boxes:details:v2"
    cached_result = get_cached(cache_key)

    if cached_result is not None:
        return {"boxes": cached_result, "source": "cache"}

    results = run_query('''
        SELECT
            b.id,
            b.name,
            b.exposure,
            b.model,
            b.created_at,
            b.updated_at,
            b.last_measurement_at,
            r.country,
            r.region,
            ST_X(b.location) AS longitude,
            ST_Y(b.location) AS latitude,
            COALESCE(
                json_agg(
                    json_build_object(
                        '_id', s.id,
                        'boxes_id', s.box_id,
                        'lastMeasurement', s.last_measurement,
                        'sensorType', s.sensor_type,
                        'title', s.title,
                        'unit', s.unit
                    )
                    ORDER BY s.id
                ) FILTER (WHERE s.id IS NOT NULL),
                '[]'::json
            ) AS sensors
        FROM boxes b
        LEFT JOIN regions r ON r.id = b.region_id
        LEFT JOIN sensors s ON s.box_id = b.id
        WHERE s.last_measurement IS NOT NULL
        GROUP BY b.id, r.country, r.region
        ORDER BY b.id
    ''')

    box_results = [format_box(row) for row in results]
    set_cached(cache_key, box_results)
    return {"boxes": box_results, "source": "database"}


@app.get("/countries")
def get_countries():
    cache_key = "countries:details"
    cached_result = get_cached(cache_key)

    if cached_result is not None:
        return {"countries": cached_result, "source": "cache"}

    results = run_query('''
        SELECT DISTINCT r.country, r.region
        FROM regions r
        JOIN boxes b ON r.id = b.region_id
        JOIN sensors s ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
        ORDER BY r.country, r.region;
    ''')

    countries_by_name = {}
    for row in results:
        country = row["country"]
        region = row["region"]
        countries_by_name.setdefault(country, {"country": country, "regions": []})["regions"].append(region)

    country_results = list(countries_by_name.values())
    set_cached(cache_key, country_results)
    return {"countries": country_results, "source": "database"}


@app.get("/tags")
def get_tags():
    tag_results, source = get_cached_list(TAGS_CACHE_KEY, TAGS_QUERY, Tag, "sensor_type")
    return {"tags": tag_results, "source": source}


@app.get("/phenomena")
def get_phenomena():
    phenomenon_results, source = get_cached_list(PHENOMENA_CACHE_KEY, PHENOMENA_QUERY, Phenomenon, "title")
    return {"phenomena": phenomenon_results, "source": source}


@app.get("/exposure")
def get_exposure():
    exposure_results, source = get_cached_list(EXPOSURE_CACHE_KEY, EXPOSURE_QUERY, Exposure, "exposure")
    return {"exposure": exposure_results, "source": source}

@app.get("/regions/{country}/{region}/measurements")
def get_region_measurements(
    country: str,
    region: str,
    filters: Annotated[CommonMeasurementFilters, Depends(measurement_filter_params)],
):
    rows = get_region_measurement_rows(country, region, filters)
    return build_region_measurement_payload(country, region, filters, rows)


def export_job_payload(job_id):
    # Only 404 when we can positively confirm the job was never submitted.
    # If Redis itself is unreachable, key_exists() returns None ("unknown")
    # and we fall through to the normal Celery lookup rather than risk a
    # false 404 for a real, in-flight job.
    if key_exists(f"{EXPORT_JOB_KEY_PREFIX}{job_id}") is False:
        return 404, {
            "error": {
                "code": 404,
                "message": f"No export job found for id '{job_id}'.",
            }
        }

    result = get_export_result(job_id)
    state = result.state
    meta = result.info if isinstance(result.info, dict) else {}

    if state == "SUCCESS":
        export_result = result.result if isinstance(result.result, dict) else {}
        object_name = export_result.get("objectName")
        download_url = get_presigned_download_url(object_name) if object_name else None

        return 200, {
            "jobId": job_id,
            "status": "completed",
            "export": {
                **export_result,
                "downloadUrl": download_url,
            },
        }

    if state == "FAILURE":
        return 500, {
            "error": {
                "code": 500,
                "message": "The export job failed.",
                "details": {
                    "jobId": job_id,
                    "reason": str(result.info),
                },
            }
        }

    status_by_state = {
        "PENDING": "queued",
        "RECEIVED": "queued",
        "STARTED": "running",
        "RETRY": "retrying",
        "PROGRESS": "running",
    }
    status = status_by_state.get(state, state.lower())
    if status in ("queued", "running", "retrying") and not export_workers_available():
        return 503, {
            "error": {
                "code": 503,
                "message": "No export worker is currently available. This job cannot progress until a worker is running.",
                "details": {
                    "jobId": job_id,
                    "status": status,
                },
            }
        }

    return 202, {
        "jobId": job_id,
        "status": status,
        "details": meta,
        "statusUrl": f"/exports/{job_id}",
    }


@app.post("/regions/{country}/{region}/measurements/exports", status_code=202)
def create_region_measurements_export(
    country: str,
    region: str,
    filters: Annotated[CommonMeasurementFilters, Depends(measurement_filter_params)],
    file_format: Annotated[ExportFormat, Query(alias="format")] = "csv",
):
    get_validated_measurement_filters(country, region, filters)
    ensure_export_dependencies_available()
    task = send_export_task(
        "exports.region_measurements",
        kwargs={
            "country": country,
            "region": region,
            "filters_data": serialize_measurement_filters(filters),
            "file_format": file_format,
        },
    )
    mark_export_job_submitted(task.id)

    return {
        "jobId": task.id,
        "status": "queued",
        "statusUrl": f"/exports/{task.id}",
        "downloadUrl": f"/exports/{task.id}/download",
    }


@app.post("/aoi/validate")
def validate_aoi(file: UploadFile = File(...)):
    content = file.file.read()
    return validate_aoi_file(file.filename or "", content)


@app.post("/aoi/measurements")
def get_aoi_measurements(
    file: Annotated[UploadFile, File(...)],
    from_date: Annotated[str, Form(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Form(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    tags: Annotated[list[str] | None, Form()] = None,
    phenomena: Annotated[list[str] | None, Form()] = None,
    exposure: Annotated[list[str] | None, Form()] = None,
    aggregate: Annotated[MeasurementAggregate, Form()] = "daily",
):
    content = file.file.read()
    aoi = validate_aoi_file(file.filename or "", content)
    filters = build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate)
    rows = get_aoi_measurement_rows(aoi["geometry"], filters)
    return build_aoi_measurement_payload(aoi, filters, rows)


@app.post("/aoi/measurements/exports", status_code=202)
def create_aoi_measurements_export(
    file: Annotated[UploadFile, File(...)],
    from_date: Annotated[str, Form(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to_date: Annotated[str, Form(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    tags: Annotated[list[str] | None, Form()] = None,
    phenomena: Annotated[list[str] | None, Form()] = None,
    exposure: Annotated[list[str] | None, Form()] = None,
    aggregate: Annotated[MeasurementAggregate, Form()] = "daily",
    file_format: Annotated[ExportFormat, Form(alias="format")] = "csv",
):
    content = file.file.read()
    aoi = validate_aoi_file(file.filename or "", content)
    filters = build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate)
    validate_measurement_filters(filters)
    ensure_export_dependencies_available()
    task = send_export_task(
        "exports.aoi_measurements",
        kwargs={
            "aoi_name": aoi["properties"]["name"],
            "geometry": aoi["geometry"],
            "filters_data": serialize_measurement_filters(filters),
            "file_format": file_format,
        },
    )
    mark_export_job_submitted(task.id)

    return {
        "jobId": task.id,
        "status": "queued",
        "statusUrl": f"/exports/{task.id}",
        "downloadUrl": f"/exports/{task.id}/download",
    }

@app.get("/exports/{job_id}")
def get_export_job(job_id: str):
    status_code, payload = export_job_payload(job_id)
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/exports/{job_id}/download")
def download_export_job(job_id: str):
    result = get_export_result(job_id)

    if result.state != "SUCCESS":
        status_code, payload = export_job_payload(job_id)
        return JSONResponse(status_code=status_code, content=payload)

    export_result = result.result if isinstance(result.result, dict) else {}
    object_name = export_result.get("objectName")
    if not object_name:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "The export completed, but no file was recorded for this job.",
                "jobId": job_id,
            },
        )

    return RedirectResponse(get_presigned_download_url(object_name))
