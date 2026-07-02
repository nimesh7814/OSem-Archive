import os
import re
import tempfile
import time as time_module
from datetime import date, datetime, time
from typing import Annotated, Literal

import geopandas
from sqlalchemy import column
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BeforeValidator
from connection import execute_query
from db_query import (
    get_common_filters, get_countries, get_summary, get_boxes,
    get_exposures, get_phenomena, get_tags, get_boxes_by_aoi, get_filter_values,
)

# Initialize FastAPI app
app = FastAPI()

base_dir = os.path.dirname(os.path.abspath(__file__))
ALLOWED_AOI_EXTENSIONS = {".zip", ".kml", ".geojson"}

# Pydantic's plain `date` type is lenient: it happily accepts a full
# ISO datetime string like "2017-01-01T00:00:00" and silently truncates it
# to the date part. Since from_date/to_date must be exactly YYYY-MM-DD,
# reject anything that doesn't match that shape before it ever reaches
# pydantic's own (more permissive) parser.
_DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_date_only(value):
    if isinstance(value, str) and not _DATE_ONLY_PATTERN.match(value):
        raise ValueError("Date must be in YYYY-MM-DD format.")
    return value


# Query(...) has to be embedded directly in this Annotated stack, not
# passed separately as a `= Query(...)` default -- FastAPI silently drops
# custom BeforeValidators on query params when Query() is supplied that
# way instead (confirmed: with a bare `x: StrictDate = Query(...)` where
# StrictDate omits Query, "2017-01-01T00:00:00" was accepted and quietly
# truncated instead of being rejected).
StrictDate = Annotated[date, BeforeValidator(_require_date_only), Query(...)]

# There's no cap on how wide from_date/to_date can be: get_common_filters
# caps rows per sensor via a LATERAL join (ORDER BY time DESC LIMIT N),
# which walks the time index backward from the newest matching row and
# stops -- that's bounded no matter how wide the date window is, so a wide
# range doesn't risk the runaway-row-count crash a flat, uncapped join
# used to (see MAX_RAW_MEASUREMENT_ROWS / PER_SENSOR_MEASUREMENT_ROWS in
# db_query.py).


# Reports how long every request took via a response header, in milliseconds.
@app.middleware("http")
async def add_response_time_header(request: Request, call_next):
    start = time_module.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time_module.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    return response

# Build dropdown selectors for the AOI endpoint from the DB's actual, raw
# filter values (not the capitalized display versions from get_exposures/
# get_tags/get_phenomena), so every option Swagger offers is guaranteed to
# match something filterable.
_exposure_values, _tag_values, _phenomenon_values = get_filter_values()
ExposureFilter = Literal[tuple(["all", *_exposure_values])]
TagFilter = Literal[tuple(["all", *_tag_values])]
PhenomenonFilter = Literal[tuple(["all", *_phenomenon_values])]

# Summary endpoint
@app.get("/stats")
def summary():
    return get_summary()

# All the country wise regions
@app.get("/countries")
def regions():
    return get_countries()

# Give a specific country
@app.get("/countries/{country}")
def country_data(country: str):
    return get_countries(country=country)

# Get the exposures
@app.get("/exposures")
def exposures():
    return get_exposures()

# Get all the phenomenas
@app.get("/phenomena")
def phenomena():
    return get_phenomena()

# Get the tags
@app.get("/tags")
def tags():
    return get_tags()

# All Boxes endpoint
@app.get("/boxes")
def boxes():
    return get_boxes()
 
# Get a One Box ID
@app.get("/boxes/{box_id}")
def box(box_id: str):
    result = get_boxes(box_id=box_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Box not found")
    return result

# Run a query to get boxes by AOI
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

    # get_boxes_by_aoi resolves its geometry path against backend/ (see
    # upload_file.py's base_dir), so save the upload there under a
    # throwaway name and hand it just the relative filename.
    fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=base_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(await file.read())

        # to_date is a plain date, but measurements are timestamptz -- push
        # it to the end of that day so the whole day is included instead of
        # cutting off at midnight.
        to_date_end = datetime.combine(to_date, time.max)

        try:
            return get_boxes_by_aoi(
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