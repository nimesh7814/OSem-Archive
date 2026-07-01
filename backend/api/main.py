from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile

from api.functions import boxes, countries, exports, root, sensors, stats
from api.functions.celery_config import celery_app
from api.functions.db_con import close_pool


app = FastAPI()


@app.on_event("shutdown")
def shutdown():
    close_pool()


@app.get("/")
def read_root():
    return root.read_root()


# main.py keeps route wiring only. The implementation lives in api.functions.

@app.get("/stats")
def get_stats():
    return stats.get_stats()


@app.get("/countries")
def get_countries():
    return countries.list_countries()


@app.get("/countries/{country}")
def get_regions_in_country(country: str):
    return countries.list_regions(country)


@app.get("/regions")
def get_regions(country: Optional[str] = Query(None)):
    return countries.list_regions_catalog(country)


@app.get("/sensors")
def get_phenomena():
    return sensors.list_sensors()


@app.get("/phenomena")
def get_legacy_phenomena():
    return sensors.list_phenomena()


@app.get("/exposures")
def get_exposures():
    return sensors.list_exposures()


@app.get("/boxes")
def get_boxes():
    return boxes.list_boxes()


@app.get("/boxes/region/{region}")
def get_boxes_by_region(
    region: str,
    filters: boxes.BoxFilters = Depends(boxes.box_filters_query),
):
    return boxes.list_boxes_by_region(region, filters)


@app.get("/boxes/region")
def get_boxes_by_country_region(
    country: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    filters: boxes.BoxFilters = Depends(boxes.box_filters_query),
):
    if not region:
        raise HTTPException(status_code=400, detail="region is required; querying an entire country is not supported")

    return boxes.list_boxes_by_country_region(country, region, filters)


@app.post("/boxes/aoi")
@app.get("/boxes/aoi")
def get_boxes_by_aoi(
    area: Optional[UploadFile] = File(None),
    file_upload: Optional[UploadFile] = File(None, alias="file"),
    geometry: Optional[str] = Form(None),
    filters: boxes.BoxFilters = Depends(boxes.box_filters_query),
):
    return boxes.list_boxes_by_aoi(area or file_upload, geometry, filters)


@app.get("/boxes/{box_id}")
def get_box(box_id: str):
    return boxes.get_box(box_id)


@app.get("/boxes/{box_id}/sensors")
def get_box_sensors(box_id: str):
    return boxes.list_box_sensors(box_id)


@app.post("/exports/region/{region}", status_code=202)
def create_export_by_region(
    region: str,
    filters: exports.ExportFilters = Depends(exports.export_filters_query),
    aggregate: str = Query("hourly"),
    format: Optional[str] = Query(None, description="'geojson' or 'csv'"),
    file_type: Optional[str] = Query(None, description="Frontend alias for format"),
):
    return exports.create_region_export(region, filters, aggregate, exports.normalize_file_format(format, file_type))


@app.post("/exports/aoi", status_code=202)
def create_export_by_aoi(
    area: Optional[UploadFile] = File(None),
    file_upload: Optional[UploadFile] = File(None, alias="file"),
    geometry: Optional[str] = Form(None),
    filters: exports.ExportFilters = Depends(exports.export_filters_form),
    aggregate: Optional[str] = Query(None),
    aggregate_form: Optional[str] = Form(None, alias="aggregate"),
    format: Optional[str] = Query(None, description="'geojson' or 'csv'"),
    format_form: Optional[str] = Form(None, alias="format"),
    file_type: Optional[str] = Query(None, description="Frontend alias for format"),
):
    selected_aggregate = aggregate or aggregate_form or "hourly"
    selected_format = exports.normalize_file_format(format or format_form, file_type)
    return exports.create_aoi_export(area or file_upload, geometry, filters, selected_aggregate, selected_format)


@app.post("/exports", status_code=202)
def create_export(
    country: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    area: Optional[UploadFile] = File(None),
    file_upload: Optional[UploadFile] = File(None, alias="file"),
    geometry: Optional[str] = Form(None),
    filters: exports.ExportFilters = Depends(exports.export_filters_query),
    aggregate: str = Query("hourly"),
    format: Optional[str] = Query(None, description="'geojson' or 'csv'"),
    file_type: Optional[str] = Query(None, description="Frontend alias for format"),
):
    selected_format = exports.normalize_file_format(format, file_type)
    if area or file_upload or geometry:
        return exports.create_aoi_export(area or file_upload, geometry, filters, aggregate, selected_format)

    if not region:
        raise HTTPException(status_code=400, detail="region is required when no AOI file or geometry is provided; querying an entire country is not supported")

    filters.country = country or filters.country
    return exports.create_region_export(region, filters, aggregate, selected_format)


@app.get("/exports")
def list_export_jobs():
    return exports.list_export_jobs()


@app.get("/exports/{job_id}")
def get_export_job(job_id: str):
    return exports.get_export_job(job_id)


@app.get("/exports/{job_id}/download")
def download_export(job_id: str):
    return exports.download_export(job_id)


@app.delete("/exports/{job_id}", status_code=204)
def delete_export_job(job_id: str):
    return exports.delete_export_job(job_id)
