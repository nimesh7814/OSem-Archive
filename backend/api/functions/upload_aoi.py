import io
import json
import geojson

import geopandas as gpd
from fastapi import HTTPException, UploadFile
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely.validation import make_valid


# Allowed file types
ALLOWED_UPLOAD_TYPES = {".geojson", ".kml", ".zip"}


# File or Area of Interest needs to be provided
async def load_aoi_geometry(file: UploadFile | None, geometry: str | None):
    
    # If both given give an error
    if file and geometry:
        raise HTTPException(400, "Provide either a file (.geojson/.kml/.zip) or draw a geometry")
    
    # If neither given give an error
    if not file and not geometry:
        raise HTTPException(400, "Provide either a file (.geojson/.kml/.zip) or draw a geometry")
    
    # If a file is given load the geometry from the file
    if file:
        return await load_uploaded_geometry(file)
    
    # if a geometry is none of the drawing
    if geometry is None:
        raise HTTPException(400, "Provide either a file (.geojson/.kml/.zip) or draw a geometry")
    return load_drawn_geometry(geometry)



async def load_uploaded_geometry(file: UploadFile):
    name = (file.filename or "").lower()
    ext = next((e for e in ALLOWED_UPLOAD_TYPES if name.endswith(e)), None)
    if ext is None:
        raise HTTPException(
            400,
            f"Unsupported file type '{name}'. Allowed: {', '.join(sorted(ALLOWED_UPLOAD_TYPES))}",
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

    geom = make_valid(unary_union(gdf.geometry))
    if geom.is_empty:
        raise HTTPException(422, "Geometry resolved to an empty shape")
    return geom


def load_drawn_geometry(geometry: str):
    try:
        parsed = geojson.loads(geometry)
    except json.JSONDecodeError:
        raise HTTPException(400, "'geometry' is not valid JSON")

    if parsed.get("type") not in ("Polygon", "MultiPolygon"):
        raise HTTPException(
            422,
            f"Drawn geometry must be a Polygon or MultiPolygon - got '{parsed.get('type')}'",
        )

    try:
        geom = shape(parsed)
    except Exception:
        raise HTTPException(400, "'geometry' is not a valid GeoJSON geometry")

    geom = make_valid(geom)
    if geom.is_empty:
        raise HTTPException(422, "Geometry resolved to an empty shape")
    return geom
