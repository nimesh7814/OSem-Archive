import json

from fastapi import HTTPException, UploadFile
from shapely.geometry import shape
from shapely.validation import make_valid

from .boxes_export import _respond, query_boxes_aggregated
from .spatial_upload import validate_and_load_geometry


async def boxes_by_aoi(
    file: UploadFile | None,
    geometry: str | None,
    country: str | None,
    region: str | None,
    exposure: str | None,
    phenomenon: str | None,
    sensor_type: str | None,
    from_date: str | None,
    to_date: str | None,
    download: bool,
    file_type: str,
    aggregate: str,
):
    if file and geometry:
        raise HTTPException(400, "Provide either 'file' or 'geometry', not both")
    if not file and not geometry:
        raise HTTPException(400, "Provide either 'file' (.geojson/.kml/.zip) or 'geometry' (GeoJSON)")
    if to_date and not from_date:
        raise HTTPException(400, "from_date is required when to_date is provided")
    if download and file_type not in ("geojson", "csv"):
        raise HTTPException(400, "file_type must be 'geojson' or 'csv'")
    if aggregate not in ("raw", "date", "month", "year"):
        raise HTTPException(400, "aggregate must be one of: raw, date, month, year")

    if file:
        geom = await validate_and_load_geometry(file)
    else:
        try:
            parsed = json.loads(geometry)
        except json.JSONDecodeError:
            raise HTTPException(400, "'geometry' is not valid JSON")
        if parsed.get("type") not in ("Polygon", "MultiPolygon"):
            raise HTTPException(
                422,
                f"Drawn geometry must be a Polygon or MultiPolygon — got '{parsed.get('type')}'",
            )
        try:
            geom = shape(parsed)
        except Exception:
            raise HTTPException(400, "'geometry' is not a valid GeoJSON geometry")
        geom = make_valid(geom)

    if geom.is_empty:
        raise HTTPException(422, "Geometry resolved to an empty shape")

    rows = await query_boxes_aggregated(
        geometry_wkt=geom.wkt,
        country=country,
        region=region,
        exposure=exposure,
        phenomenon=phenomenon,
        sensor_type=sensor_type,
        from_date=from_date,
        to_date=to_date,
        aggregate=aggregate,
    )
    return _respond(rows, download, file_type, "boxes_aoi")
