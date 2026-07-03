import os
import re
import tempfile
import time as time_module
from datetime import date, datetime, time
from typing import Annotated, Literal, Optional
from urllib.parse import parse_qsl, urlencode
from uuid import UUID

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import PlainTextResponse, Response
from pydantic import BeforeValidator
from starlette.convertors import Convertor, register_url_convertor
from functions.api_reference import API_REFERENCE_TEXT
from functions.db_query import (
    get_countries, get_summary, get_boxes,
    get_exposures, get_phenomena, get_tags, get_boxes_by_aoi, get_boxes_by_region,
    get_filter_values,
)
from functions.http_cache import get_cached_body, set_cached_body
from functions.export import (
    create_aoi_export,
    create_region_export,
    delete_export_job,
    download_export_job,
    get_export_job,
    list_export_jobs,
)
from functions.upload_file import base_dir as aoi_upload_dir

# Initialize FastAPI app
app = FastAPI()

# Restricts {box_id} to 24 hex chars so it can't swallow /boxes/region or /boxes/aoi.
class BoxIdConvertor(Convertor):
    regex = "[0-9a-fA-F]{24}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value

register_url_convertor("box_id", BoxIdConvertor())

ALLOWED_AOI_EXTENSIONS = {".zip", ".kml", ".geojson"}

# Date Validation
_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_date_only(value):
    if isinstance(value, str) and not _DATE_ONLY_PATTERN.match(value):
        raise ValueError("Date must be in YYYY-MM-DD format.")
    return value


# StrictDate is a date that must be provided
StrictDate = Annotated[date, BeforeValidator(_require_date_only), Query(...)]


# Reports how long every request took via a response header, in milliseconds.
@app.middleware("http")
async def add_response_time_header(request: Request, call_next):
    start = time_module.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time_module.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    return response


# Caches GET responses in Redis for 24h; /exports is excluded since job status must stay live.
_UNCACHED_PREFIXES = ("/exports", "/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def cache_get_responses(request: Request, call_next):
    if request.method != "GET" or request.url.path == "/" or request.url.path.startswith(_UNCACHED_PREFIXES):
        return await call_next(request)

    cache_key = f"{request.url.path}?{urlencode(sorted(parse_qsl(request.url.query)))}"

    cached_body = get_cached_body(cache_key)
    if cached_body is not None:
        return Response(content=cached_body, media_type="application/json", headers={"X-Cache": "HIT"})

    response = await call_next(request)
    if response.status_code != 200:
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    set_cached_body(cache_key, body.decode("utf-8"))

    headers = {
        name: value for name, value in response.headers.items()
        if name.lower() not in ("content-length", "content-type")
    }
    headers["X-Cache"] = "MISS"
    return Response(content=body, status_code=response.status_code, media_type=response.media_type, headers=headers)

# Raw DB values, not the capitalized display versions, so filters match exactly.
_exposure_values, _tag_values, _phenomenon_values = get_filter_values()
ExposureFilter = Literal[tuple(["all", *_exposure_values])]
TagFilter = Literal[tuple(["all", *_tag_values])]
PhenomenonFilter = Literal[tuple(["all", *_phenomenon_values])]

# Export job aggregate tier / output format selectors
AggregateFilter = Literal["raw", "hourly", "daily", "monthly", "yearly"]
FormatFilter = Literal["csv", "geojson"]

@app.get("/", response_class=PlainTextResponse)
def root():
    return API_REFERENCE_TEXT

@app.get("/stats")
def get_stats():
    return get_summary()

@app.get("/countries")
def list_countries():
    return get_countries()

@app.get("/countries/{country}")
def get_country_regions(country: str):
    return get_countries(country=country)

@app.get("/exposures")
def exposures():
    return get_exposures()

@app.get("/phenomena")
def phenomena():
    return get_phenomena()

@app.get("/tags")
def tags():
    return get_tags()

@app.get("/boxes")
def boxes():
    return get_boxes()

@app.get("/boxes/{box_id:box_id}")
def box(box_id: str):
    result = get_boxes(box_id=box_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Box '{box_id}' not found")
    return result

@app.post("/boxes/aoi")
async def boxes_by_aoi(
    from_date: StrictDate,
    to_date: StrictDate,
    file: UploadFile = File(...),
    exposure: ExposureFilter = Form("all"),  # type: ignore[valid-type]
    tags: TagFilter = Form("all"),  # type: ignore[valid-type]
    phenomenon: PhenomenonFilter = Form("all"),  # type: ignore[valid-type]
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_AOI_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported AOI file type '{ext}'. Use .zip, .kml, or .geojson.",
        )

    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must be on or after from_date.")

    start = time_module.perf_counter()

    # Written into upload_file.py's own directory since that's where it reads from.
    fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=aoi_upload_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(await file.read())

        to_date_end = datetime.combine(to_date, time.max)

        try:
            result = get_boxes_by_aoi(
                os.path.basename(tmp_path),
                from_date,
                to_date_end,
                exposure=exposure,
                tags=tags,
                phenomenon=phenomenon,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        os.remove(tmp_path)

    result["response_time_ms"] = round((time_module.perf_counter() - start) * 1000, 2)
    return result

@app.get("/boxes/region")
def boxes_by_region(
    region: str,
    from_date: StrictDate,
    to_date: StrictDate,
    exposure: ExposureFilter = Query("all"),  # type: ignore[valid-type]
    tags: TagFilter = Query("all"),  # type: ignore[valid-type]
    phenomenon: PhenomenonFilter = Query("all"),  # type: ignore[valid-type]
):
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must be on or after from_date.")

    start = time_module.perf_counter()
    to_date_end = datetime.combine(to_date, time.max)

    result = get_boxes_by_region(
        region,
        from_date,
        to_date_end,
        exposure=exposure,
        tags=tags,
        phenomenon=phenomenon,
    )

    if result["geometry"] is None:
        raise HTTPException(status_code=404, detail=f"Region '{region}' not found")

    return {
        "region": result["region"],
        "createdAt": datetime.now().astimezone(),
        "response_time_ms": round((time_module.perf_counter() - start) * 1000, 2),
        "geometry": result["geometry"],
        "boxes": result["boxes"],
    }

# Creates an export job: give either region or an AOI file, not both. POST since it has side effects.
@app.post("/exports", status_code=202)
async def create_export(
    from_date: StrictDate,
    to_date: StrictDate,
    region: Optional[str] = Query(None),
    file: Optional[UploadFile] = File(None),
    aggregate: AggregateFilter = Query("hourly"),
    format: FormatFilter = Query("geojson"),
    exposure: ExposureFilter = Query("all"),  # type: ignore[valid-type]
    tags: TagFilter = Query("all"),  # type: ignore[valid-type]
    phenomenon: PhenomenonFilter = Query("all"),  # type: ignore[valid-type]
):
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must be on or after from_date.")

    if region and file is not None:
        raise HTTPException(status_code=400, detail="Provide either region or an AOI file, not both.")

    to_date_end = datetime.combine(to_date, time.max)

    if file is not None:
        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_AOI_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported AOI file type '{ext}'. Use .zip, .kml, or .geojson.",
            )

        # Original filename, used for the download's Content-Disposition name.
        source_name = os.path.splitext(os.path.basename(file.filename or "aoi"))[0]

        fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=aoi_upload_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(await file.read())

            return create_aoi_export(
                os.path.basename(tmp_path), from_date, to_date_end,
                aggregate, format, exposure, tags, phenomenon,
                source_name=source_name,
            )
        finally:
            os.remove(tmp_path)

    if not region:
        raise HTTPException(status_code=400, detail="Either region or an AOI file must be provided.")

    return create_region_export(region, from_date, to_date_end, aggregate, format, exposure, tags, phenomenon)

@app.get("/exports")
def list_exports():
    return list_export_jobs()

# job_id: UUID rejects malformed ids with a 422 instead of a raw DB error.
@app.get("/exports/{job_id}")
def get_export_status(job_id: UUID):
    job = get_export_job(str(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail=f"Export job '{job_id}' not found")
    return job

@app.get("/exports/{job_id}/download")
def download_export(job_id: UUID):
    return download_export_job(str(job_id))

@app.delete("/exports/{job_id}", status_code=204)
def delete_export(job_id: UUID):
    delete_export_job(str(job_id))


# Hides tags/phenomenon's huge enum from Swagger docs only; validation is unaffected.
_HIDDEN_ENUM_PARAMS = {"tags", "phenomenon"}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )

    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            for param in operation.get("parameters", []):
                if param.get("name") in _HIDDEN_ENUM_PARAMS:
                    param.get("schema", {}).pop("enum", None)

    for component_schema in schema.get("components", {}).get("schemas", {}).values():
        for prop_name, prop_schema in component_schema.get("properties", {}).items():
            if prop_name in _HIDDEN_ENUM_PARAMS:
                prop_schema.pop("enum", None)
                for sub_schema in prop_schema.get("allOf", []):
                    sub_schema.pop("enum", None)

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi

