from datetime import datetime
import io
import csv
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi import FastAPI, Query, UploadFile, File, HTTPException, Body
from pydantic import BaseModel, field_validator
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
import psycopg2
import os

load_dotenv()

# ── Earliest date in the dataset ─────────────────────────────────────────────
DATASET_START_DATE = "2014-06-03"

app = FastAPI()


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5436"),
        database=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


# ── KML helpers ───────────────────────────────────────────────────────────────

# KML namespace used by Google Earth / standard KML files
_KML_NS = "http://www.opengis.net/kml/2.2"


def _coord_str_to_ring(coord_text: str) -> list[list[float]]:
    """Convert a KML <coordinates> text block into a GeoJSON ring list."""
    ring = []
    for token in coord_text.strip().split():
        parts = token.split(",")
        if len(parts) >= 2:
            ring.append([float(parts[0]), float(parts[1])])
    return ring


def _kml_polygon_element_to_geojson(polygon_el: ET.Element) -> dict:
    """
    Convert a single KML <Polygon> element into a GeoJSON Polygon geometry.
    Handles outer ring + optional inner rings (holes).
    """
    rings = []

    outer = polygon_el.find(f".//{{{_KML_NS}}}outerBoundaryIs//{{{_KML_NS}}}coordinates")
    if outer is None or not outer.text:
        raise ValueError("KML Polygon has no outerBoundaryIs/coordinates")
    rings.append(_coord_str_to_ring(outer.text))

    for inner in polygon_el.findall(f".//{{{_KML_NS}}}innerBoundaryIs//{{{_KML_NS}}}coordinates"):
        if inner.text:
            rings.append(_coord_str_to_ring(inner.text))

    return {"type": "Polygon", "coordinates": rings}


def _parse_kml(kml_bytes: bytes) -> dict:
    """
    Parse KML bytes and return a GeoJSON geometry (Polygon or MultiPolygon).
    Raises ValueError if no usable geometry is found.
    """
    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid KML XML: {exc}") from exc

    polygon_elements = root.findall(f".//{{{_KML_NS}}}Polygon")
    if not polygon_elements:
        raise ValueError("No <Polygon> elements found in KML file")

    polygons = [_kml_polygon_element_to_geojson(el) for el in polygon_elements]

    if len(polygons) == 1:
        return polygons[0]

    # Merge multiple Polygons into a MultiPolygon
    return {
        "type": "MultiPolygon",
        "coordinates": [p["coordinates"] for p in polygons],
    }


def _parse_geojson_file(raw_bytes: bytes) -> dict:
    """
    Accept a GeoJSON file that may be:
      - A bare Geometry      (type: Polygon / MultiPolygon)
      - A Feature            (geometry.type: Polygon / MultiPolygon)
      - A FeatureCollection  (features[0].geometry ...)

    Returns a Polygon or MultiPolygon geometry dict.
    Raises ValueError on anything else.
    """
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    geom = None
    obj_type = data.get("type")

    if obj_type in ("Polygon", "MultiPolygon"):
        geom = data
    elif obj_type == "Feature":
        geom = data.get("geometry")
    elif obj_type == "FeatureCollection":
        features = data.get("features", [])
        if not features:
            raise ValueError("FeatureCollection has no features")
        geom = features[0].get("geometry")
    else:
        raise ValueError(f"Unsupported GeoJSON type: {obj_type!r}")

    if geom is None or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("No Polygon or MultiPolygon geometry found in GeoJSON file")

    return geom


# ── /parse_area_file — upload KML or GeoJSON, get back a GeoJSON geometry ────
@app.post("/parse_area_file")
async def parse_area_file(file: UploadFile = File(...)):
    """
    Upload a .kml or .geojson / .json file.
    Returns the extracted geometry as a GeoJSON string ready to pass
    as the `aoi` query parameter to /bbox_data.

    Accepted content types / extensions:
      - application/vnd.google-earth.kml+xml  (.kml)
      - application/geo+json / application/json / text/plain (.geojson, .json)
    """
    raw = await file.read()
    filename = (file.filename or "").lower()

    try:
        if filename.endswith(".kml") or (file.content_type or "").startswith(
            "application/vnd.google-earth.kml"
        ):
            geom = _parse_kml(raw)
        elif (
            filename.endswith((".geojson", ".json"))
            or (file.content_type or "") in (
                "application/geo+json",
                "application/json",
                "text/plain",
            )
        ):
            geom = _parse_geojson_file(raw)
        else:
            raise HTTPException(
                status_code=415,
                detail=(
                    "Unsupported file type. "
                    "Upload a .kml, .geojson, or .json file."
                ),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "geometry_type": geom["type"],
        "aoi":           json.dumps(geom),   # pass this string to /bbox_data?aoi=
    }


# ── /parse_freehand_area — convert {lat,lng} point list to GeoJSON polygon ────

class LatLng(BaseModel):
    lat: float
    lng: float


class FreehandAreaRequest(BaseModel):
    points: list[LatLng]

    @field_validator("points")
    @classmethod
    def validate_points(cls, v: list[LatLng]) -> list[LatLng]:
        if len(v) < 3:
            raise ValueError("At least 3 points are required to form a polygon")
        return v


def _freehand_points_to_geojson(points: list[LatLng]) -> dict:
    """
    Convert a list of {lat, lng} map points into a GeoJSON Polygon geometry.

    GeoJSON rings use [longitude, latitude] order and must be closed
    (first and last coordinate identical). Both are handled here automatically.
    """
    ring: list[list[float]] = [[p.lng, p.lat] for p in points]

    # Close the ring if the frontend didn't
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    return {"type": "Polygon", "coordinates": [ring]}


@app.post("/parse_freehand_area")
def parse_freehand_area(body: FreehandAreaRequest):
    """
    Convert a freehand-drawn area (array of {lat, lng} map points) into a
    GeoJSON Polygon. Returns the same { geometry_type, aoi } shape as
    /parse_area_file so the frontend can pass `aoi` straight to /bbox_data.

    Request body:
        { "points": [{"lat": 51.2, "lng": 7.1}, ...] }

    Minimum 3 points required. The ring is closed automatically if needed.
    """
    geom = _freehand_points_to_geojson(body.points)
    return {
        "geometry_type": geom["type"],
        "aoi":           json.dumps(geom),
    }


# ── /bbox_data — bbox OR drawn/imported polygon/multipolygon ─────────────────
@app.get("/bbox_data")
def bbox_data(
    # bbox (provide all 4 OR use aoi — not both)
    min_lon:     float | None = Query(None,                description="Min longitude e.g. 6.0"),
    min_lat:     float | None = Query(None,                description="Min latitude  e.g. 50.0"),
    max_lon:     float | None = Query(None,                description="Max longitude e.g. 9.0"),
    max_lat:     float | None = Query(None,                description="Max latitude  e.g. 52.0"),
    # aoi — GeoJSON Polygon or MultiPolygon geometry string
    # Accepted from:
    #   - Polygon drawing   → GeoJSON string sent directly by the frontend
    #   - Freehand drawing  → POST /parse_freehand_area, pass back the `aoi` field
    #   - KML / GeoJSON import → POST /parse_area_file, pass back the `aoi` field
    aoi:         str | None   = Query(None,                description="GeoJSON Polygon or MultiPolygon geometry string"),
    # common filters
    from_date:   str          = Query(DATASET_START_DATE,  description="Start date YYYY-MM-DD"),
    to_date:     str | None   = Query(None,                description="End date   YYYY-MM-DD"),
    sensor_type: str          = Query("all",               description="'all' or comma-separated categories"),
    download:    bool         = Query(False,               description="Set true to download zip"),
):
    has_bbox = all(v is not None for v in [min_lon, min_lat, max_lon, max_lat])
    has_aoi  = aoi is not None

    if not has_bbox and not has_aoi:
        return {
            "error": (
                "Provide either bbox params (min_lon, min_lat, max_lon, max_lat), "
                "or an `aoi` GeoJSON string. The `aoi` string can come from: "
                "a drawn Polygon on the frontend, "
                "POST /parse_freehand_area (freehand drawing), or "
                "POST /parse_area_file (KML / GeoJSON file import)."
            )
        }
    # When a drawn polygon is sent, the frontend includes the bbox of that polygon
    # alongside the `aoi` geometry string. Prefer the precise `aoi` shape and
    # silently ignore the bbox so the frontend doesn't need to change.
    if has_bbox and has_aoi:
        has_bbox = False

    effective_from = from_date or DATASET_START_DATE
    effective_to   = to_date or datetime.today().strftime("%Y-%m-%d")

    selected_categories = (
        None
        if sensor_type.lower() == "all"
        else [s.strip() for s in sensor_type.split(",")]
    )

    filters = ["sf.csv_url IS NOT NULL"]
    params: list = []
    label = ""

    # ── Spatial filter: bbox ──────────────────────────────────────────────────
    if has_bbox:
        if not (
            -180 <= min_lon <= 180
            and -180 <= max_lon <= 180
            and -90  <= min_lat <= 90
            and -90  <= max_lat <= 90
        ):
            return {"error": "Coordinate out of valid range."}
        if min_lon >= max_lon or min_lat >= max_lat:
            return {"error": "min values must be less than max values."}

        filters.append(
            "ST_Within(st.location::geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
        )
        params.extend([min_lon, min_lat, max_lon, max_lat])
        label = f"bbox_{min_lon}_{min_lat}_{max_lon}_{max_lat}"

    # ── Spatial filter: aoi (drawn polygon, custom area, or imported file) ────
    if has_aoi:
        # Tolerates bare Geometry, Feature, or FeatureCollection — all are unwrapped
        # to a plain Polygon / MultiPolygon so PostGIS can consume them directly.
        try:
            geom = json.loads(aoi)
            obj_type = geom.get("type")

            if obj_type == "Feature":
                # Unwrap Feature wrapper (sent by e.g. Leaflet draw, Mapbox draw)
                geom = geom.get("geometry") or {}
                obj_type = geom.get("type")
            elif obj_type == "FeatureCollection":
                features = geom.get("features") or []
                if not features:
                    raise ValueError("FeatureCollection has no features")
                geom = (features[0].get("geometry")) or {}
                obj_type = geom.get("type")

            if obj_type not in ("Polygon", "MultiPolygon"):
                raise ValueError(
                    f"Expected Polygon or MultiPolygon geometry, got {obj_type!r}"
                )

            aoi_geojson = json.dumps(geom)
        except (json.JSONDecodeError, ValueError) as exc:
            return {"error": f"Invalid aoi: {exc}"}

        filters.append(
            "ST_Within(st.location::geometry, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
        )
        params.append(aoi_geojson)
        label = "aoi_custom"

    # ── Sensor category filter ────────────────────────────────────────────────
    if selected_categories:
        placeholders = ", ".join(["%s"] * len(selected_categories))
        filters.append(f"se.category IN ({placeholders})")
        params.extend(selected_categories)

    where_clause = " AND ".join(filters)

    conn   = get_db_connection()
    cursor = conn.cursor()

    try:
        # ── Summary mode ──────────────────────────────────────────────────────
        if not download:
            # Build the spatial predicate once as a CTE so PostGIS can use the
            # GIST index on st.location and evaluate the geometry only once.
            if has_bbox:
                spatial_cte = (
                    "spatial_filter AS ("
                    "  SELECT st_id FROM stations"
                    "  WHERE ST_Within(location::geometry,"
                    "        ST_MakeEnvelope(%s, %s, %s, %s, 4326))"
                    ")"
                )
                spatial_params: list = [min_lon, min_lat, max_lon, max_lat]
            else:
                spatial_cte = (
                    "spatial_filter AS ("
                    "  SELECT st_id FROM stations"
                    "  WHERE ST_Within(location::geometry,"
                    "        ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))"
                    ")"
                )
                spatial_params = [aoi_geojson]

            # Optional sensor-category filter clause (reused in two sub-queries)
            if selected_categories:
                cat_placeholders = ", ".join(["%s"] * len(selected_categories))
                cat_filter = f"AND se.category IN ({cat_placeholders})"
                cat_params = selected_categories
            else:
                cat_filter = ""
                cat_params = []

            summary_query = f"""
            WITH {spatial_cte},

            -- Aggregate readings only for matched stations + date range
            reading_agg AS (
                SELECT
                    se.st_id,
                    COUNT(DISTINCT se.se_id)                              AS total_sensors,
                    COALESCE(SUM(re.count), 0)                            AS total_readings,
                    STRING_AGG(DISTINCT se.category, ', '
                               ORDER BY se.category)                      AS categories,
                    MIN(re.date)                                           AS first_date,
                    MAX(re.date)                                           AS last_date
                FROM sensors se
                JOIN readings re ON re.se_id = se.se_id
                WHERE se.st_id IN (SELECT st_id FROM spatial_filter)
                  AND re.date BETWEEN %s AND %s
                  {cat_filter}
                GROUP BY se.st_id
            ),

            -- Sum file sizes separately to avoid join fan-out
            file_agg AS (
                SELECT st_id, COALESCE(SUM(size_mb), 0) AS total_size_mb
                FROM sensor_files
                WHERE st_id IN (SELECT st_id FROM spatial_filter)
                GROUP BY st_id
            )

            SELECT
                st.st_id,
                st.name,
                ST_Y(st.location::geometry)  AS lat,
                ST_X(st.location::geometry)  AS lng,
                st.country,
                st.region,
                ra.total_sensors,
                ra.total_readings,
                COALESCE(fa.total_size_mb, 0) AS total_size_mb,
                ra.categories,
                ra.first_date,
                ra.last_date
            FROM stations st
            JOIN reading_agg ra ON ra.st_id = st.st_id
            LEFT JOIN file_agg fa ON fa.st_id = st.st_id
            ORDER BY st.country, st.name;
            """

            reading_params: list = [
                *spatial_params,
                effective_from, effective_to,
                *cat_params,
            ]

            cursor.execute(summary_query, reading_params)
            rows = cursor.fetchall()

            if not rows:
                return {"message": "No stations found for the given area and date range."}

            area_info = (
                {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}
                if has_bbox
                else {"type": "aoi_custom"}
            )

            return {
                "area":           area_info,
                "period":         {"from": effective_from, "to": effective_to},
                "total_stations": len(rows),
                "total_sensors":  sum(r[6] for r in rows),
                "total_readings": sum(int(r[7]) for r in rows),
                "total_size_mb":  round(sum(float(r[8]) for r in rows), 2),
                "stations": [
                    {
                        "st_id":          r[0],
                        "name":           r[1],
                        "lat":            float(r[2]),
                        "lng":            float(r[3]),
                        "country":        r[4],
                        "region":         r[5],
                        "total_sensors":  r[6],
                        "total_readings": int(r[7]),
                        "total_size_mb":  round(float(r[8]), 2),
                        "categories":     r[9],
                        "first_date":     str(r[10]),
                        "last_date":      str(r[11]),
                    }
                    for r in rows
                ],
            }

        # ── Download mode ─────────────────────────────────────────────────────
        download_query = f"""
        SELECT
            st.st_id,
            st.name     AS station_name,
            st.exposure,
            st.model,
            ST_Y(st.location::geometry) AS latitude,
            ST_X(st.location::geometry) AS longitude,
            st.country,
            st.region,
            se.se_id,
            se.title    AS sensor_title,
            se.type     AS sensor_type,
            se.category,
            se.unit,
            sf.date,
            sf.csv_url,
            sf.size_mb
        FROM sensor_files sf
        JOIN sensors  se ON sf.se_id = se.se_id
        JOIN stations st ON sf.st_id = st.st_id
        WHERE sf.date BETWEEN %s AND %s
          AND {where_clause}
        ORDER BY st.country, st.name, se.category, sf.date;
        """

        cursor.execute(download_query, (effective_from, effective_to, *params))
        rows = cursor.fetchall()

        if not rows:
            return {"message": "No data found for the given area and date range."}

        return _build_zip_response(rows, label)

    finally:
        conn.close()


@app.get("/")
def home():
    return {"message": "OK"}


@app.get("/summary")
def get_summary():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stations")
        total_stations = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM sensors")
        total_sensors = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(count) FROM readings")
        total_readings = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT (country)) FROM stations WHERE country IS NOT NULL")
        total_countries = cursor.fetchone()[0]
    finally:
        conn.close()

    return {
        "stations":  total_stations,
        "sensors":   total_sensors,
        "readings":  total_readings,
        "countries": total_countries,
    }


@app.get("/countries")
def get_countries():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT country FROM stations WHERE country IS NOT NULL")
        countries = [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()
    return {"countries": countries}


@app.get("/regions")
def get_regions(country: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT region FROM stations WHERE country = %s AND region IS NOT NULL",
            (country,),
        )
        regions = [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()
    return {"regions": regions}


@app.get("/sensor_categories")
def get_sensor_categories():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT category FROM sensors WHERE category IS NOT NULL")
        categories = [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()
    return {"categories": categories}


@app.get("/get_stations")
def get_stations():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                st.st_id,
                st.name,
                st.country,
                st.region,
                ST_Y(st.location::geometry) AS latitude,
                ST_X(st.location::geometry) AS longitude,
                COUNT(se.se_id)             AS total_sensors,
                SUM(re.count)               AS total_readings,
                STRING_AGG(DISTINCT se.category, ', ') AS categories
            FROM stations st
            JOIN sensors  se ON st.st_id = se.st_id
            JOIN readings re ON se.se_id = re.se_id
            WHERE st.country IS NOT NULL
            GROUP BY st.st_id, st.name, st.country, st.region, st.location
            ORDER BY st.country, st.region, st.name;
        """)
        stations = [
            {
                "st_id":          row[0],
                "name":           row[1],
                "country":        row[2],
                "region":         row[3],
                "latitude":       float(row[4]) if row[4] is not None else None,
                "longitude":      float(row[5]) if row[5] is not None else None,
                "total_sensors":  row[6],
                "total_readings": row[7],
                "categories":     row[8],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()
    return {"stations": stations}


# ── /country_region_data — country/region filters only, no bbox, no aoi ──────
@app.get("/country_region_data")
def country_region_data(
    sensor_type: str      = Query("all", description="'all' or comma-separated e.g. 'humidity,temperature'"),
    from_date:   str|None = Query(None,  description="Start date YYYY-MM-DD"),
    to_date:     str|None = Query(None,  description="End date   YYYY-MM-DD"),
    country:     str|None = Query(None,  description="Country filter e.g. 'Germany'"),
    region:      str|None = Query(None,  description="Region filter  e.g. 'Bavaria'"),
    download:    bool     = Query(False, description="Set true to download a zip"),
):
    effective_from = from_date or DATASET_START_DATE
    effective_to   = to_date   or datetime.today().strftime('%Y-%m-%d')

    selected_categories = (
        None if sensor_type.lower() == "all"
        else [s.strip() for s in sensor_type.split(",")]
    )

    filters = ["st.country IS NOT NULL", "sf.csv_url IS NOT NULL"]
    params  = []

    if country:
        filters.append("st.country = %s")
        params.append(country)
    if region:
        filters.append("st.region = %s")
        params.append(region)
    if selected_categories:
        placeholders = ", ".join(["%s"] * len(selected_categories))
        filters.append(f"se.category IN ({placeholders})")
        params.extend(selected_categories)

    where_clause = " AND ".join(filters)
    label = f"{country}_{region}" if country and region else country or "all"

    conn   = get_db_connection()
    cursor = conn.cursor()

    try:
        # ── Summary mode ──────────────────────────────────────────────────────
        if not download:
            summary_query = f"""
            SELECT
                st.country,
                st.region,
                COUNT(DISTINCT st.st_id) AS station_count,
                COUNT(DISTINCT se.se_id) AS sensor_count,
                COALESCE(SUM(sf.size_mb), 0) AS total_size_mb
            FROM sensor_files sf
            JOIN sensors  se ON sf.se_id = se.se_id
            JOIN stations st ON sf.st_id = st.st_id
            WHERE sf.date BETWEEN %s AND %s
              AND {where_clause}
            GROUP BY st.country, st.region
            ORDER BY st.country, st.region;
            """
            cursor.execute(summary_query, (effective_from, effective_to, *params))
            rows = cursor.fetchall()

            if not rows:
                return {"message": "No data found for the given filters."}

            return {
                "summary": [
                    {
                        "country":       r[0],
                        "region":        r[1],
                        "station_count": r[2],
                        "sensor_count":  r[3],
                        "total_size_mb": round(float(r[4]), 2),
                    }
                    for r in rows
                ]
            }

        # ── Download mode ─────────────────────────────────────────────────────
        download_query = f"""
        SELECT
            st.st_id,
            st.name     AS station_name,
            st.exposure,
            st.model,
            ST_Y(st.location::geometry) AS latitude,
            ST_X(st.location::geometry) AS longitude,
            st.country,
            st.region,
            se.se_id,
            se.title    AS sensor_title,
            se.type     AS sensor_type,
            se.category,
            se.unit,
            sf.date,
            sf.csv_url,
            sf.size_mb
        FROM sensor_files sf
        JOIN sensors  se ON sf.se_id = se.se_id
        JOIN stations st ON sf.st_id = st.st_id
        WHERE sf.date BETWEEN %s AND %s
          AND {where_clause}
        ORDER BY st.country, st.region, se.category, st.st_id, se.se_id, sf.date;
        """

        cursor.execute(download_query, (effective_from, effective_to, *params))
        rows = cursor.fetchall()

        if not rows:
            return {"message": "No data found for the given filters."}

        return _build_zip_response(rows, label)

    finally:
        conn.close()


# ── Shared helper: pack rows into a zip StreamingResponse ────────────────────
def _build_zip_response(rows: list, label: str) -> StreamingResponse:
    total_size_mb = sum(r[15] or 0 for r in rows)
    total_files   = len(rows)

    tsv_buffer = io.StringIO()
    tsv_writer = csv.writer(tsv_buffer, delimiter="\t")
    tsv_writer.writerow([
        "st_id", "station_name", "exposure", "model",
        "latitude", "longitude", "country", "region",
        "se_id", "sensor_title", "sensor_type", "category", "unit",
        "date", "csv_url", "size_mb",
    ])
    for r in rows:
        tsv_writer.writerow([
            r[0],  r[1],  r[2],  r[3],
            r[4],  r[5],  r[6],  r[7],
            r[8],  r[9],  r[10], r[11], r[12],
            r[13], r[14], r[15] or 0,
        ])
    urls_txt = tsv_buffer.getvalue().encode("utf-8")

    downloader_dir = Path(__file__).parent / "downloader"
    downloader_py  = (downloader_dir / "osem_downloader.py").read_bytes()
    requirements   = (downloader_dir / "requirements.txt").read_bytes()
    readme         = (downloader_dir / "README.md").read_bytes()

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("urls.txt",           urls_txt)
        zf.writestr("osem_downloader.py", downloader_py)
        zf.writestr("requirements.txt",   requirements)
        zf.writestr("README.md",          readme)
    zip_buffer.seek(0)

    generated = datetime.today().strftime('%Y-%m-%d_%H-%M-%S')

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=osem_{label}_{generated}.zip",
            "X-Total-Files":       str(total_files),
            "X-Total-Size-MB":     f"{total_size_mb:.2f}",
        },
    )


@app.get("/station_readings")
def station_readings(
    st_id: str = Query(..., description="Station ID"),
    month: int = Query(..., description="Month as integer e.g. 5"),
    year:  int = Query(..., description="Year  as integer e.g. 2026"),
):
    conn   = get_db_connection()
    cursor = conn.cursor()

    try:
        # ── 1. Station metadata ───────────────────────────────────────────────
        cursor.execute("""
            SELECT
                st.st_id,
                st.name,
                ST_Y(st.location::geometry) AS latitude,
                ST_X(st.location::geometry) AS longitude,
                COUNT(DISTINCT se.se_id)    AS total_sensors,
                SUM(re.count)               AS total_readings,
                STRING_AGG(DISTINCT se.category, ', ') AS categories
            FROM stations st
            JOIN sensors  se ON st.st_id = se.st_id
            JOIN readings re ON se.se_id = re.se_id
            WHERE st.st_id = %s
            GROUP BY st.st_id, st.name, st.location;
        """, (st_id,))

        row = cursor.fetchone()
        if not row:
            return {"error": f"Station '{st_id}' not found."}

        station = {
            "st_id":          row[0],
            "name":           row[1],
            "latitude":       float(row[2]) if row[2] is not None else None,
            "longitude":      float(row[3]) if row[3] is not None else None,
            "total_sensors":  row[4],
            "total_readings": int(row[5]) if row[5] is not None else 0,
            "categories":     row[6],
        }

        # ── 2. Daily avg per sensor category ─────────────────────────────────
        cursor.execute("""
            SELECT
                EXTRACT(DAY FROM re.date)::int       AS day,
                se.category,
                ROUND(AVG(re.avg_value)::numeric, 2) AS avg_value,
                ROUND(AVG(re.min_value)::numeric, 2) AS min_value,
                ROUND(AVG(re.max_value)::numeric, 2) AS max_value
            FROM readings re
            JOIN sensors se ON re.se_id = se.se_id
            WHERE se.st_id = %s
              AND EXTRACT(MONTH FROM re.date) = %s
              AND EXTRACT(YEAR  FROM re.date) = %s
              AND se.category IS NOT NULL
            GROUP BY day, se.category
            ORDER BY day, se.category;
        """, (st_id, month, year))

        chart_data: dict[str, list] = {}
        for day, category, avg, mn, mx in cursor.fetchall():
            if category not in chart_data:
                chart_data[category] = []
            chart_data[category].append({
                "day": day,
                "avg": float(avg) if avg is not None else None,
                "min": float(mn)  if mn  is not None else None,
                "max": float(mx)  if mx  is not None else None,
            })

        # ── 3. Per-sensor summary cards (window fn replaces correlated subquery)
        cursor.execute("""
            SELECT
                se.se_id,
                se.title,
                se.category,
                se.unit,
                ROUND(AVG(re.avg_value)::numeric, 2) AS avg_value,
                FIRST_VALUE(ROUND(re.avg_value::numeric, 2))
                    OVER (
                        PARTITION BY se.se_id
                        ORDER BY re.date DESC
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS latest_value
            FROM readings re
            JOIN sensors se ON re.se_id = se.se_id
            WHERE se.st_id = %s
              AND EXTRACT(MONTH FROM re.date) = %s
              AND EXTRACT(YEAR  FROM re.date) = %s
            GROUP BY se.se_id, se.title, se.category, se.unit, re.avg_value, re.date
            ORDER BY se.category, se.se_id;
        """, (st_id, month, year))

        seen: set = set()
        sensors = []
        for row in cursor.fetchall():
            if row[0] in seen:
                continue
            seen.add(row[0])
            sensors.append({
                "se_id":    row[0],
                "title":    row[1],
                "category": row[2],
                "unit":     row[3],
                "avg":      float(row[4]) if row[4] is not None else None,
                "latest":   float(row[5]) if row[5] is not None else None,
            })

    finally:
        conn.close()

    return {
        "station":    station,
        "period":     {"month": month, "year": year},
        "chart_data": chart_data,
        "sensors":    sensors,
    }
