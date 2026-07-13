import logging
import os
import time as time_module
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

try:
    from . import export_jobs, filters, measurements
    from .aoi import validate_aoi_file
    from .bucket import check_object_storage_connection, get_presigned_download_url
    from .cache import check_cache_connection, get_cached, set_cached
    from .celery_app import ensure_export_workers_available, send_export_task, get_export_result
    from .db import check_database_connection, run_query
    from .errors import register_exception_handlers
    from .format import format_box
    from .schema import CommonMeasurementFilters, ExportFormat, MeasurementAggregate, Summary
except ImportError:
    import export_jobs
    import filters
    import measurements
    from aoi import validate_aoi_file
    from bucket import check_object_storage_connection, get_presigned_download_url
    from cache import check_cache_connection, get_cached, set_cached
    from celery_app import ensure_export_workers_available, send_export_task, get_export_result
    from db import check_database_connection, run_query
    from errors import register_exception_handlers
    from format import format_box
    from schema import CommonMeasurementFilters, ExportFormat, MeasurementAggregate, Summary

app = FastAPI()
register_exception_handlers(app)

request_logger = logging.getLogger("api.requests")
SERVICE_STATUS_CACHE_SECONDS = int(os.getenv("SERVICE_STATUS_CACHE_SECONDS", "5"))
_dependency_status_cache = {"checked_at": 0.0, "checks": None}


def get_dependency_checks_cached():
    now = time_module.monotonic()
    cached_checks = _dependency_status_cache["checks"]
    checked_at = _dependency_status_cache["checked_at"]
    if cached_checks is not None and (now - checked_at) < SERVICE_STATUS_CACHE_SECONDS:
        return cached_checks

    checks = dependency_checks()
    _dependency_status_cache["checked_at"] = now
    _dependency_status_cache["checks"] = checks
    return checks


def is_service_degraded():
    checks = get_dependency_checks_cached()
    return any(item["status"] != "ok" for item in checks.values())


async def annotate_json_response_with_service_status(request: Request, response):
    if request.url.path in {"/", "/health"}:
        return response

    if is_service_degraded():
        return JSONResponse(
            status_code=503,
            content={"status": "degraded, further information available at /health"},
        )

    return response


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

    response = await annotate_json_response_with_service_status(request, response)

    duration_ms = (time_module.perf_counter() - started_at) * 1000
    request_logger.info(
        "HTTP request completed method=%s path=%s status_code=%s duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# Health
def service_status(name, check):
    try:
        check()
    except Exception as exc:
        return name, {"status": "unavailable", "message": str(exc)}
    return name, {"status": "ok"}


def dependency_checks():
    redis_name, redis_status = service_status("redis", check_cache_connection)
    minio_name, minio_status = service_status("minio", check_object_storage_connection)
    database_name, database_status = service_status("database", check_database_connection)
    checks = {
        redis_name: redis_status,
        minio_name: minio_status,
        database_name: database_status,
    }

    if redis_status["status"] == "ok":
        celery_name, celery_status = service_status("celeryWorker", ensure_export_workers_available)
        checks[celery_name] = celery_status
    else:
        checks["celeryWorker"] = {
            "status": "unavailable",
            "message": "The export worker cannot be checked because Redis is unavailable.",
        }

    return checks


@app.get("/")
def health_check():
    checks = get_dependency_checks_cached()
    status_code = 200 if all(item["status"] == "ok" for item in checks.values()) else 503
    return JSONResponse(status_code=status_code, content={
        "status": "ok" if status_code == 200 else "degraded, further information available at /health",
    })


@app.get("/health")
def dependency_health_check():
    checks = get_dependency_checks_cached()
    status_code = 200 if all(item["status"] == "ok" for item in checks.values()) else 503
    return JSONResponse(status_code=status_code, content={
        "status": "ok" if status_code == 200 else "degraded",
        "dependencies": checks,
    })


# Summary of the data
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
    cache_key = "boxes:details"
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


@app.get("/countries/{country}")
def get_country(country: str):
    cache_key = f"countries:{country}"
    cached_result = get_cached(cache_key)
    if cached_result is not None:
        return {"country": country, "regions": cached_result, "source": "cache"}

    results = run_query('''
        SELECT DISTINCT r.region
        FROM regions r
        JOIN boxes b ON r.id = b.region_id
        JOIN sensors s ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
          AND r.country = %s
        ORDER BY r.region;
    ''', (country,))

    if not results:
        raise HTTPException(status_code=404, detail=f"Country '{country}' not found")

    regions = [row["region"] for row in results]
    set_cached(cache_key, regions)
    return {"country": country, "regions": regions, "source": "database"}


@app.get("/tags")
def get_tags():
    tag_results, source = filters.get_tags()
    return {"tags": tag_results, "source": source}


@app.get("/phenomena")
def get_phenomena():
    phenomenon_results, source = filters.get_phenomena()
    return {"phenomena": phenomenon_results, "source": source}


@app.get("/exposure")
def get_exposure():
    exposure_results, source = filters.get_exposure()
    return {"exposure": exposure_results, "source": source}


# Region measurements
@app.get("/regions/{country}/{region}/measurements")
def get_region_measurements(
    country: str,
    region: str,
    request_filters: Annotated[CommonMeasurementFilters, Depends(filters.daily_measurement_filter_params)],
    count_aggregate: Annotated[MeasurementAggregate | None, Query(alias="aggregate")] = None,
):
    # aggregate here only selects what to count in recordCount - boxes data is always daily.
    return measurements.get_region_measurements_response(country, region, request_filters, count_aggregate)


@app.post("/regions/{country}/{region}/measurements/exports", status_code=202)
def create_region_measurements_export(
    country: str,
    region: str,
    request_filters: Annotated[CommonMeasurementFilters, Depends(filters.measurement_filter_params)],
    file_format: Annotated[ExportFormat, Query(alias="format")] = "csv",
):
    measurements.get_validated_measurement_filters(country, region, request_filters)
    export_jobs.ensure_export_dependencies_available()
    task = send_export_task(
        "exports.region_measurements",
        kwargs={
            "country": country,
            "region": region,
            "filters_data": filters.serialize_measurement_filters(request_filters),
            "file_format": file_format,
        },
    )
    export_jobs.mark_export_job_submitted(task.id)

    return {
        "jobId": task.id,
        "status": "queued",
        "statusUrl": f"/exports/{task.id}",
        "downloadUrl": f"/exports/{task.id}/download",
    }


# Area-of-interest (AOI) measurements
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
    count_aggregate: Annotated[MeasurementAggregate | None, Form(alias="aggregate")] = None,
):
    # aggregate here only selects what to count in recordCount - boxes data is always daily.
    content = file.file.read()
    aoi = validate_aoi_file(file.filename or "", content)
    request_filters = filters.build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, "daily")
    return measurements.get_aoi_measurements_response(aoi, request_filters, count_aggregate)


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
    request_filters = filters.build_common_measurement_filters(from_date, to_date, tags, phenomena, exposure, aggregate)
    filters.validate_measurement_filters(request_filters)
    export_jobs.ensure_export_dependencies_available()
    task = send_export_task(
        "exports.aoi_measurements",
        kwargs={
            "aoi_name": aoi["properties"]["name"],
            "geometry": aoi["geometry"],
            "filters_data": filters.serialize_measurement_filters(request_filters),
            "file_format": file_format,
        },
    )
    export_jobs.mark_export_job_submitted(task.id)

    return {
        "jobId": task.id,
        "status": "queued",
        "statusUrl": f"/exports/{task.id}",
        "downloadUrl": f"/exports/{task.id}/download",
    }


# Export jobs
@app.get("/exports/{job_id}")
def get_export_job(job_id: str):
    status_code, payload = export_jobs.export_job_payload(job_id)
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/exports/{job_id}/download")
def download_export_job(job_id: str):
    result = get_export_result(job_id)
    if result.state != "SUCCESS":
        status_code, payload = export_jobs.export_job_payload(job_id)
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
