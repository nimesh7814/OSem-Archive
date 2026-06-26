from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Query, UploadFile
from fastapi.responses import PlainTextResponse

try:
    from .functions import boxes, boxes_aoi, boxes_region, exports, phenomena, regions, root
    from .functions.boxes import BoxQueryParams, box_query_params
    from .functions.db_con import close_pool
except ImportError:
    from functions import boxes, boxes_aoi, boxes_region, exports, phenomena, regions, root
    from functions.boxes import BoxQueryParams, box_query_params
    from functions.db_con import close_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_pool()


app = FastAPI(title="openSenseMap Archive API", lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
async def root_endpoint():
    return root.list_routes(app)


@app.get("/regions")
async def list_regions(country: str | None = Query(None)):
    return await regions.list_regions(country)


@app.get("/boxes")
async def list_boxes(
    box_id: str | None = Query(None, description="Comma-separated box IDs; omit for all boxes"),
):
    return await boxes.list_boxes(box_id)


@app.get("/boxes/region")
async def list_boxes_by_region(
    country: str | None = Query(None),
    region: str | None = Query(None),
    filters: BoxQueryParams = Depends(box_query_params),
):
    return await boxes_region.list_boxes_by_region(country, region, filters)


@app.post("/boxes/aoi")
async def boxes_by_aoi(
    file: UploadFile | None = File(None),
    geometry: str | None = Form(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    filters: BoxQueryParams = Depends(box_query_params),
):
    return await boxes_aoi.boxes_by_aoi(file, geometry, country, region, filters)


@app.get("/phenomena")
async def list_phenomena():
    return await phenomena.list_phenomena()


@app.post("/exports", status_code=202)
async def create_export(
    file_type: str = Query("geojson", description="'geojson' or 'csv'"),
    country: str | None = Query(None),
    region: str | None = Query(None),
    exposure: str | None = Query(None),
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    aggregate: str = Query("hourly"),
    box_id: str | None = Query(None, description="Comma-separated box IDs"),
    geometry_wkt: str | None = Query(None, description="WKT geometry for spatial filter"),
):
    return await exports.create_export(
        file_type, country, region, exposure, phenomenon, sensor_type,
        from_date, to_date, aggregate, box_id, geometry_wkt,
    )


@app.get("/exports")
async def list_export_jobs():
    return await exports.list_export_jobs()


@app.get("/exports/{job_id}/download")
async def download_export(job_id: str):
    return await exports.download_export(job_id)


@app.get("/exports/{job_id}")
async def get_export_job(job_id: str):
    return await exports.get_export_job(job_id)


@app.delete("/exports/{job_id}", status_code=204)
async def delete_export_job(job_id: str):
    return await exports.delete_export_job(job_id)
