from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import PlainTextResponse

from api.functions import boxes, boxes_aoi, boxes_region, exports, phenomena, regions, root
from api.functions.db_con import close_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_pool()


app = FastAPI(title="openSenseMap Archive API", lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
async def root_endpoint():
    """List all available endpoints."""
    return root.list_routes(app)


@app.get("/regions")
async def list_regions(country: str | None = Query(None)):
    """Show all regions where data is available. If country is given, show that country's regions."""
    return await regions.list_regions(country)


@app.get("/boxes")
async def list_boxes(
    box_id: str | None = Query(None, description="Comma-separated box IDs; omit for all boxes"),
):
    """Visualize boxes on the map. Returns all boxes by default; box_id narrows to specific boxes."""
    return await boxes.list_boxes(box_id)


@app.get("/boxes/region")
async def list_boxes_by_region(
    country: str | None = Query(None),
    region: str | None = Query(None),
    exposure: str | None = Query(None),
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    download: bool = Query(False),
    file_type: str = Query("geojson"),
    aggregate: str = Query("raw"),
):
    """Main query path: boxes filtered by a given region or country."""
    return await boxes_region.list_boxes_by_region(
        country, region, exposure, phenomenon, sensor_type,
        from_date, to_date, download, file_type, aggregate,
    )


@app.post("/boxes/aoi")
async def boxes_by_aoi(
    file: UploadFile | None = File(None),
    geometry: str | None = Form(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    exposure: str | None = Query(None),
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    download: bool = Query(False),
    file_type: str = Query("geojson"),
    aggregate: str = Query("raw"),
):
    """
    Query boxes within an area of interest. Accepts either an uploaded file
    (.geojson, .kml, or .zip shapefile) or a GeoJSON geometry drawn on the map.
    """
    return await boxes_aoi.boxes_by_aoi(
        file, geometry, country, region, exposure, phenomenon, sensor_type,
        from_date, to_date, download, file_type, aggregate,
    )


@app.get("/phenomena")
async def list_phenomena():
    """Distinct sensor titles/types in the DB, for populating a filter dropdown."""
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
    aggregate: str = Query("raw"),
    box_id: str | None = Query(None, description="Comma-separated box IDs"),
    geometry_wkt: str | None = Query(None, description="WKT geometry for spatial filter"),
):
    """Kick off an async export. Poll GET /exports/{job_id} until status is 'done', then download."""
    return await exports.create_export(
        file_type, country, region, exposure, phenomenon, sensor_type,
        from_date, to_date, aggregate, box_id, geometry_wkt,
    )


@app.get("/exports")
async def list_export_jobs():
    """List all export jobs, newest first."""
    return await exports.list_export_jobs()


@app.get("/exports/{job_id}/download")
async def download_export(job_id: str):
    """Stream the completed export file. Returns 202 if the job is still running."""
    return await exports.download_export(job_id)


@app.get("/exports/{job_id}")
async def get_export_job(job_id: str):
    """Get status and metadata for a single export job."""
    return await exports.get_export_job(job_id)


@app.delete("/exports/{job_id}", status_code=204)
async def delete_export_job(job_id: str):
    """Remove an export job and delete its file."""
    return await exports.delete_export_job(job_id)
