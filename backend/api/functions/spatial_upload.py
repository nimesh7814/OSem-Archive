"""
api/functions/spatial_upload.py

Validates and normalises an uploaded geometry file (.geojson, .kml, .zip shapefile).
Returns a single Shapely Polygon/MultiPolygon in EPSG:4326 ready for spatial queries.
"""

import io

import geopandas as gpd
from fastapi import HTTPException, UploadFile
from shapely.ops import unary_union
from shapely.validation import make_valid

_ALLOWED = {".geojson", ".kml", ".zip"}


async def validate_and_load_geometry(file: UploadFile):
    name = (file.filename or "").lower()
    ext = next((e for e in _ALLOWED if name.endswith(e)), None)
    if ext is None:
        raise HTTPException(
            400,
            f"Unsupported file type '{name}'. Allowed: {', '.join(sorted(_ALLOWED))}",
        )

    content = await file.read()
    try:
        if ext == ".kml":
            gdf = gpd.read_file(io.BytesIO(content), driver="KML")
        else:
            gdf = gpd.read_file(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(400, f"Could not parse geometry file: {exc}") from exc

    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        raise HTTPException(422, "No Polygon or MultiPolygon geometry found in file")

    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    return make_valid(unary_union(gdf.geometry))
