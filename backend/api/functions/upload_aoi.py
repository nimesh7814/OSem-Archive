import json
import geojson
import os
import shutil
import tempfile
import zipfile
from typing import Optional

import geopandas as gpd
from fastapi import HTTPException, UploadFile
from shapely.errors import ShapelyError
from shapely.geometry import shape


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB raw upload (geojson/kml/zip)
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB guard against zip bombs


def validate_geojson_content(raw_bytes: bytes):
    """Reject malformed GeoJSON before handing it to GeoPandas/Shapely."""
    try:
        data = geojson.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="File not valid: not valid JSON.")

    if not isinstance(data, dict) or "type" not in data:
        raise HTTPException(status_code=400, detail="File not valid: missing GeoJSON 'type' field.")

    geojson_type = data.get("type")

    try:
        if geojson_type == "FeatureCollection":
            features = data.get("features")
            if not features or not isinstance(features, list):
                raise HTTPException(status_code=400, detail="File not valid: FeatureCollection has no features.")
            for feature in features:
                geom = feature.get("geometry")
                if not geom:
                    continue
                geom_obj = shape(geom)
                if not geom_obj.is_valid:
                    raise HTTPException(status_code=400, detail="File not valid: contains an invalid geometry.")

        elif geojson_type == "Feature":
            geom = data.get("geometry")
            if not geom:
                raise HTTPException(status_code=400, detail="File not valid: feature missing geometry.")
            geom_obj = shape(geom)
            if not geom_obj.is_valid:
                raise HTTPException(status_code=400, detail="File not valid: invalid geometry.")

        elif geojson_type in (
            "Point", "MultiPoint", "LineString", "MultiLineString",
            "Polygon", "MultiPolygon", "GeometryCollection",
        ):
            geom_obj = shape(data)
            if not geom_obj.is_valid:
                raise HTTPException(status_code=400, detail="File not valid: invalid geometry.")

        else:
            raise HTTPException(status_code=400, detail="File not valid: unrecognized GeoJSON type.")

    except HTTPException:
        raise
    except (ShapelyError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="File not valid: malformed geometry.")


def extract_polygons(geom):
    if geom is None:
        return []
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return [geom]
    if geom.geom_type == "GeometryCollection":
        polygons = []
        for part in geom.geoms:
            polygons.extend(extract_polygons(part))
        return polygons
    return []


def polygons_to_wkt(polygons, crs="EPSG:4326") -> str:
    if not polygons:
        raise HTTPException(status_code=400, detail="File not valid: no polygon geometry found.")

    combined_geom = gpd.GeoSeries(polygons, crs=crs).union_all()

    if combined_geom is None or combined_geom.is_empty:
        raise HTTPException(status_code=400, detail="File not valid: resulting geometry is empty.")

    return combined_geom.wkt


def geojson_bytes_to_wkt(raw_bytes: bytes, source_label: str = "File") -> str:
    """Convert GeoJSON bytes from uploads or drawn map geometry into WKT."""
    validate_geojson_content(raw_bytes)

    try:
        data = json.loads(raw_bytes)
        geojson_type = data.get("type")

        if geojson_type == "FeatureCollection":
            geoms = [
                shape(f["geometry"])
                for f in data.get("features", [])
                if f.get("geometry")
            ]
        elif geojson_type == "Feature":
            geoms = [shape(data["geometry"])] if data.get("geometry") else []
        else:
            geoms = [shape(data)]

        gdf = gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")

        if gdf.empty:
            raise HTTPException(status_code=400, detail=f"{source_label} not valid: contains no geometries.")

        polygons = []
        for geom in gdf.geometry:
            polygons.extend(extract_polygons(geom))

        return polygons_to_wkt(polygons, crs=gdf.crs)

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail=f"{source_label} not valid: could not parse geometry.")


def load_geometry_from_geojson_string(geometry: str) -> str:
    """Convert frontend-drawn GeoJSON geometry into WKT for PostGIS."""
    if not geometry or not geometry.strip():
        raise HTTPException(status_code=400, detail="Missing AOI geometry.")

    return geojson_bytes_to_wkt(geometry.encode("utf-8"), source_label="Geometry")


def load_geometry_from_upload_or_geometry(
    file: Optional[UploadFile] = None,
    geometry: Optional[str] = None,
) -> str:
    """Accept either an uploaded AOI file or a drawn GeoJSON geometry."""
    if file is not None and file.filename:
        return load_geometry_from_upload(file)

    if geometry:
        return load_geometry_from_geojson_string(geometry)

    raise HTTPException(status_code=400, detail="Upload an AOI file or provide drawn AOI geometry.")


def load_geometry_from_upload(file: UploadFile) -> str:
    """Convert uploaded AOI files into WKT for PostGIS ST_GeomFromText."""
    filename = file.filename or ""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext not in ("geojson", "kml", "zip"):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .geojson, .kml, or zipped shapefile (.zip).",
        )

    raw_bytes = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (limit 50 MB).")

    if ext == "geojson":
        validate_geojson_content(raw_bytes)

    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_path = os.path.join(tmp_dir, filename)
        with open(tmp_path, "wb") as f:
            f.write(raw_bytes)

        if ext == "zip":
            extract_dir = os.path.join(tmp_dir, "extracted")
            try:
                with zipfile.ZipFile(tmp_path, "r") as z:
                    total_uncompressed = sum(member.file_size for member in z.infolist())
                    if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                        raise HTTPException(status_code=413, detail="Zip contents too large once extracted.")

                    extract_root = os.path.realpath(extract_dir)
                    for member in z.infolist():
                        member_path = os.path.realpath(os.path.join(extract_dir, member.filename))
                        if not (member_path == extract_root or member_path.startswith(extract_root + os.sep)):
                            raise HTTPException(status_code=400, detail="File not valid: unsafe path in zip archive.")

                    z.extractall(extract_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="File not valid: not a valid zip archive.")

            shp_files = [
                os.path.join(extract_dir, f)
                for f in os.listdir(extract_dir)
                if f.lower().endswith(".shp")
            ]
            if not shp_files:
                raise HTTPException(status_code=400, detail="File not valid: no .shp file found inside the zip.")
            gdf = gpd.read_file(shp_files[0])

            if gdf.crs is None:
                raise HTTPException(
                    status_code=400,
                    detail="File not valid: shapefile has no coordinate reference system (.prj) defined.",
                )
            if gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)

        elif ext == "kml":
            gdf = gpd.read_file(tmp_path, driver="KML")

        else:
            return geojson_bytes_to_wkt(raw_bytes)

        if gdf.empty:
            raise HTTPException(status_code=400, detail="File not valid: contains no geometries.")

        polygons = []
        for geom in gdf.geometry:
            polygons.extend(extract_polygons(geom))

        return polygons_to_wkt(polygons, crs=gdf.crs)

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="File not valid: could not parse the uploaded file.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
