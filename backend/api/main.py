from fastapi import FastAPI, Query

from app.api import routes_download

app = FastAPI(title="openSenseMap Archive API")

app.include_router(routes_download.router)


# Health
@app.get("/health")
async def health():
    pass


# Discovery
@app.get("/regions")
async def list_regions(country: str | None = Query(None)):
    pass


@app.get("/boxes")
async def list_boxes(
    bbox: str | None = Query(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    exposure: str | None = Query(None), 
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    format: str = Query("json"), 
):
    pass


@app.get("/boxes/{box_id}")
async def get_box(box_id: str):
    pass


@app.get("/boxes/{box_id}/sensors")
async def get_box_sensors(box_id: str):
    pass


@app.get("/phenomena")
async def list_phenomena():
    pass



# Measurements
@app.get("/boxes/{box_id}/sensors/{sensor_id}/measurements")
async def get_sensor_measurements(
    box_id: str,
    sensor_id: str,
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    limit: int | None = Query(None),
):
    pass


# Exports
@app.post("/exports")
async def create_export(
    format: str = Query(...), 
    bbox: str | None = Query(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    box_id: str | None = Query(None), 
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    exposure: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    columns: str | None = Query(None),
):
    pass


@app.get("/exports/{job_id}")
async def get_export_status(job_id: str):
    pass


@app.get("/exports/{job_id}/download")
async def download_export(job_id: str):
    pass


@app.get("/exports")
async def list_exports(
    status: str | None = Query(None),
    limit: int | None = Query(None),
):
    pass


@app.delete("/exports/{job_id}")
async def delete_export(job_id: str):
    pass