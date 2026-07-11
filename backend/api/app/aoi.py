import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import geojson
import shapefile

from .db import DatabaseQueryError, run_query

SUPPORTED_POLYGON_TYPES = ["Polygon", "MultiPolygon"]
NESTED_GEOJSON_TYPES = {"FeatureCollection", "Feature", "GeometryCollection"}
# Caps recursion depth in payload.is_valid()/extract_polygon_geometries() so a maliciously nested file can't trigger a RecursionError.
MAX_GEOJSON_NESTING_DEPTH = 64
UNSUPPORTED_AOI_GEOMETRY = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
}
# .prj is checked separately by validate_shapefile_prj(), which reports a missing .prj as WRONG_CRS_ERROR instead of INVALID_FILE_ERROR.
REQUIRED_SHAPEFILE_EXTENSIONS = {".shp", ".shx", ".dbf"}
NO_VALID_GEOMETRY_ERROR = "No valid Geometry"
INVALID_FILE_ERROR = "File is not valid"
WRONG_CRS_ERROR = "Coordinate System is wrong (not EPSG: 4326)."


class AoiValidationError(Exception):
    pass


def validate_aoi_file(filename, content):
    extension = Path(filename or "").suffix.lower()
    processor = AOI_PROCESSORS.get(extension)
    if processor is None:
        raise AoiValidationError(INVALID_FILE_ERROR)

    if not content:
        raise AoiValidationError(INVALID_FILE_ERROR)

    geometries = processor(content)
    merged_geometry = process_geometries(geometries)

    return {
        "type": "Feature",
        "geometry": merged_geometry,
        "properties": {
            "name": geojson_name(filename),
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "input": filename,
            "area_sqkm": geometry_area_sqkm(merged_geometry),
        },
    }


def geojson_name(filename):
    return Path(geojson_filename(filename)).stem


def geojson_filename(filename):
    name = Path(filename or "aoi").stem
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-._")
    return f"{name or 'aoi'}.geojson"


def process_geojson(content):
    try:
        payload = geojson.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AoiValidationError(INVALID_FILE_ERROR) from exc

    if not isinstance(payload, dict) or not payload.get("type"):
        raise AoiValidationError(INVALID_FILE_ERROR)
    reject_excessive_geojson_nesting(payload)
    if not payload.is_valid:
        raise AoiValidationError(INVALID_FILE_ERROR)

    validate_geojson_crs(payload)
    return extract_polygon_geometries(payload)


def reject_excessive_geojson_nesting(payload):
    # Stack-based on purpose: this runs before any recursive validation, so it must not itself recurse.
    stack = [(payload, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_GEOJSON_NESTING_DEPTH:
            raise AoiValidationError(INVALID_FILE_ERROR)
        if not isinstance(node, dict) or node.get("type") not in NESTED_GEOJSON_TYPES:
            continue

        node_type = node.get("type")
        if node_type == "FeatureCollection":
            children = node.get("features") or []
        elif node_type == "Feature":
            geometry = node.get("geometry")
            children = [geometry] if geometry is not None else []
        else:  # GeometryCollection
            children = node.get("geometries") or []

        stack.extend((child, depth + 1) for child in children)


def validate_geojson_crs(payload):
    crs = payload.get("crs") if isinstance(payload, dict) else None
    if crs is None:
        return

    crs_name = ""
    if isinstance(crs, dict):
        crs_name = str((crs.get("properties") or {}).get("name") or "")

    normalized = crs_name.upper().replace("::", ":")
    if "EPSG:4326" not in normalized and "CRS84" not in normalized:
        raise AoiValidationError(WRONG_CRS_ERROR)


def extract_polygon_geometries(payload):
    if not isinstance(payload, dict):
        return []

    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        geometries = []
        for feature in payload.get("features") or []:
            geometries.extend(extract_polygon_geometries(feature))
        return geometries

    if payload_type == "Feature":
        geometry = payload.get("geometry")
        if geometry is None:
            return []
        return extract_polygon_geometries(geometry)

    if payload_type == "GeometryCollection":
        geometries = []
        for geometry in payload.get("geometries") or []:
            geometries.extend(extract_polygon_geometries(geometry))
        return geometries

    if payload_type in SUPPORTED_POLYGON_TYPES:
        return [payload]

    if payload_type in UNSUPPORTED_AOI_GEOMETRY:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR)

    return []


def process_kml(content):
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise AoiValidationError(INVALID_FILE_ERROR) from exc

    unsupported_geometry_found = False
    geometries = []
    for polygon in root.iter():
        geometry_name = local_name(polygon.tag)
        if geometry_name in UNSUPPORTED_AOI_GEOMETRY or geometry_name in ("MultiTrack", "Track"):
            unsupported_geometry_found = True
            continue
        if geometry_name != "Polygon":
            continue

        rings = []
        for boundary_name in ("outerBoundaryIs", "innerBoundaryIs"):
            for boundary in child_elements_by_name(polygon, boundary_name):
                coordinates_node = first_descendant_by_name(boundary, "coordinates")
                if coordinates_node is None or not coordinates_node.text:
                    continue
                ring = parse_kml_coordinates(coordinates_node.text)
                if ring:
                    rings.append(close_ring(ring))

        if rings:
            geometries.append({"type": "Polygon", "coordinates": rings})

    if unsupported_geometry_found:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR)

    return geometries


def child_elements_by_name(element, name):
    return [child for child in element if local_name(child.tag) == name]


def first_descendant_by_name(element, name):
    for descendant in element.iter():
        if local_name(descendant.tag) == name:
            return descendant
    return None


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_kml_coordinates(text):
    coordinates = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            coordinates.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue
    return coordinates


def close_ring(ring):
    if ring and ring[0] != ring[-1]:
        return [*ring, ring[0]]
    return ring


def process_zip(content):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir).resolve()
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                safe_extract(archive, temp_path)
        except zipfile.BadZipFile as exc:
            raise AoiValidationError(INVALID_FILE_ERROR) from exc

        shapefiles = list(temp_path.rglob("*.shp"))
        if not shapefiles:
            raise AoiValidationError(INVALID_FILE_ERROR)
        if len(shapefiles) > 1:
            raise AoiValidationError(INVALID_FILE_ERROR)

        shp_path = shapefiles[0]
        validate_shapefile_components(shp_path)
        validate_shapefile_prj(shp_path)

        try:
            reader = shapefile.Reader(str(shp_path))
        except shapefile.ShapefileException as exc:
            raise AoiValidationError(INVALID_FILE_ERROR) from exc

        polygon_shape_types = {
            shapefile.POLYGON,
            shapefile.POLYGONM,
            shapefile.POLYGONZ,
        }

        try:
            geometries = []
            for shape in reader.shapes():
                if shape.shapeType == shapefile.NULL:
                    continue
                if shape.shapeType not in polygon_shape_types:
                    raise AoiValidationError(NO_VALID_GEOMETRY_ERROR)
                geometry = shape.__geo_interface__
                geometries.extend(extract_polygon_geometries(geometry))
            return geometries
        finally:
            reader.close()


def validate_shapefile_components(shp_path):
    missing_extensions = [
        extension
        for extension in REQUIRED_SHAPEFILE_EXTENSIONS
        if not shp_path.with_suffix(extension).exists()
    ]
    if missing_extensions:
        raise AoiValidationError(INVALID_FILE_ERROR)


def process_geometries(geometries):
    if not geometries:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR)

    for geometry in geometries:
        validate_wgs84_coordinates(geometry)

    return merge_polygons(geometries)


def safe_extract(archive, target_dir):
    for member in archive.infolist():
        destination = (target_dir / member.filename).resolve()
        if os.path.commonpath([target_dir, destination]) != str(target_dir):
            raise AoiValidationError(INVALID_FILE_ERROR)
        archive.extract(member, target_dir)


def validate_shapefile_prj(shp_path):
    prj_path = shp_path.with_suffix(".prj")
    if not prj_path.exists():
        raise AoiValidationError(WRONG_CRS_ERROR)

    prj_text = prj_path.read_text(encoding="utf-8", errors="ignore").upper()
    if "PROJCS" in prj_text or "PROJCRS" in prj_text:
        raise AoiValidationError(WRONG_CRS_ERROR)

    is_wgs84 = (
        "WGS_1984" in prj_text
        or "WGS 84" in prj_text
        or '"EPSG","4326"' in prj_text
        or "EPSG:4326" in prj_text
    )
    if not is_wgs84:
        raise AoiValidationError(WRONG_CRS_ERROR)


def validate_wgs84_coordinates(geometry):
    for coordinate in iter_coordinate_pairs(geometry.get("coordinates")):
        longitude, latitude = coordinate
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise AoiValidationError(WRONG_CRS_ERROR)


def iter_coordinate_pairs(value):
    if not isinstance(value, (list, tuple)):
        return

    if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        yield float(value[0]), float(value[1])
        return

    for item in value:
        yield from iter_coordinate_pairs(item)


def merge_polygons(geometries):
    try:
        rows = run_query('''
            WITH input AS (
                SELECT ST_SetSRID(ST_GeomFromGeoJSON(value::text), 4326) AS geom
                FROM jsonb_array_elements(%s::jsonb) AS value
            ),
            polygons AS (
                SELECT (ST_Dump(ST_CollectionExtract(ST_MakeValid(geom), 3))).geom AS geom
                FROM input
            ),
            merged AS (
                SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom
                FROM polygons
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
            ),
            normalized AS (
                SELECT CASE
                    WHEN GeometryType(geom) = 'MULTIPOLYGON' AND ST_NumGeometries(geom) = 1
                        THEN ST_GeometryN(geom, 1)
                    ELSE geom
                END AS geom
                FROM merged
            )
            SELECT ST_AsGeoJSON(geom)::json AS geometry
            FROM normalized
            WHERE geom IS NOT NULL
              AND NOT ST_IsEmpty(geom)
              AND ST_IsValid(geom)
        ''', (json.dumps(geometries),))
    except (DatabaseQueryError, ValueError, TypeError) as exc:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR) from exc

    if not rows:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR)

    geometry = rows[0]["geometry"]
    if isinstance(geometry, str):
        geometry = json.loads(geometry)
    return geometry


def geometry_area_sqkm(geometry):
    try:
        rows = run_query('''
            SELECT ST_Area(
                ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography
            ) / 1000000.0 AS area_sqkm
        ''', (json.dumps(geometry),))
    except (DatabaseQueryError, ValueError, TypeError) as exc:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR) from exc

    if not rows or rows[0]["area_sqkm"] is None:
        raise AoiValidationError(NO_VALID_GEOMETRY_ERROR)

    return round(float(rows[0]["area_sqkm"]), 6)


# Built here, after the processor functions above are defined; validate_aoi_file looks this up at call time.
AOI_PROCESSORS = {
    ".geojson": process_geojson,
    ".kml": process_kml,
    ".zip": process_zip,
}
