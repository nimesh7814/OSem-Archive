"""
OpenSenseMap — Spatial REST API  (replaces map_viewer.py)
Port: 5052

All endpoints are read-only and require no authentication.

Endpoints
---------
GET /api/stations
    ?bbox=minLon,minLat,maxLon,maxLat   (required)
    ?exposure=outdoor|indoor|mobile      (optional)
    ?box_type=fixed|mobile               (optional)
    → GeoJSON FeatureCollection of matching stations

GET /api/station/<station_id>
    → GeoJSON Feature with full station info

GET /api/station/<station_id>/sensors
    → JSON array of sensors with time-range + statistics

GET /api/sensor/<sensor_id>/data
    ?from=ISO8601                         (optional, default: earliest available)
    ?to=ISO8601                           (optional, default: now)
    ?resolution=raw|hourly|daily|monthly  (optional, default: hourly)
    → JSON {sensor_id, unit, data: [{time, avg, min, max, count}]}

GET /api/station/<station_id>/download
    ?sensor_ids=id1,id2,...              (optional, default: all sensors)
    ?from=ISO8601                        (optional)
    ?to=ISO8601                          (optional)
    ?format=geojson|shp|csv             (required)
    → file download

GET /api/sensor/<sensor_id>/download
    ?from=ISO8601                        (optional)
    ?to=ISO8601                          (optional)
    ?format=geojson|shp|csv             (required)
    → file download
"""

import io
import os
import csv
import json
import zipfile
import tempfile
import struct
import datetime

from flask import Flask, jsonify, request, Response, send_file
import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "osem_db"),
    "user":     os.getenv("DB_USER",     "osem"),
    "password": os.getenv("DB_PASSWORD", "changeme"),
}
# Read-only DB user — override with env vars if set
RO_CONFIG = {
    **DB_CONFIG,
    "user":     os.getenv("DB_RO_USER",     DB_CONFIG["user"]),
    "password": os.getenv("DB_RO_PASSWORD", DB_CONFIG["password"]),
}


def get_conn():
    return psycopg2.connect(connect_timeout=10, **RO_CONFIG)


app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt):
    return dt.isoformat() if dt else None


def _parse_bbox(raw):
    """Parse 'minLon,minLat,maxLon,maxLat' → (minLon, minLat, maxLon, maxLat) floats."""
    try:
        parts = [float(x) for x in raw.split(",")]
        if len(parts) != 4:
            raise ValueError
        return parts
    except Exception:
        return None


def _parse_dt(raw):
    if not raw:
        return None
    # Accept ISO 8601 with or without Z
    raw = raw.replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(raw)


# ---------------------------------------------------------------------------
# 1. Stations within bounding box
# ---------------------------------------------------------------------------

@app.route("/api/stations")
def stations_in_bbox():
    """
    Required: ?bbox=minLon,minLat,maxLon,maxLat
    Optional: ?exposure=  ?box_type=
    Returns:  GeoJSON FeatureCollection
    """
    raw_bbox = request.args.get("bbox")
    if not raw_bbox:
        return jsonify({"error": "bbox parameter is required (minLon,minLat,maxLon,maxLat)"}), 400

    bbox = _parse_bbox(raw_bbox)
    if bbox is None:
        return jsonify({"error": "Invalid bbox — expected minLon,minLat,maxLon,maxLat"}), 400

    min_lon, min_lat, max_lon, max_lat = bbox

    exposure = request.args.get("exposure", "").strip().lower() or None
    box_type = request.args.get("box_type", "").strip().lower() or None

    params = [min_lon, min_lat, max_lon, max_lat]
    filters = []
    if exposure:
        filters.append("LOWER(st.exposure) = %s")
        params.append(exposure)
    if box_type:
        filters.append("LOWER(st.box_type) = %s")
        params.append(box_type)

    where_extra = ("AND " + " AND ".join(filters)) if filters else ""

    sql = f"""
        WITH sensor_stats AS (
            SELECT
                s.station_id,
                COUNT(DISTINCT s.sensor_id)             AS sensor_count,
                COALESCE(SUM(h.reading_count), 0)::BIGINT AS reading_count,
                MIN(h.bucket)                            AS earliest,
                MAX(h.bucket) + INTERVAL '1 hour'       AS latest
            FROM sensors s
            LEFT JOIN sensor_data_hourly h ON h.sensor_id = s.sensor_id
            GROUP BY s.station_id
        )
        SELECT
            st.station_id,
            st.name,
            st.box_type,
            st.exposure,
            ST_X(st.geometry)  AS longitude,
            ST_Y(st.geometry)  AS latitude,
            COALESCE(ss.sensor_count,  0) AS sensor_count,
            COALESCE(ss.reading_count, 0) AS reading_count,
            ss.earliest,
            ss.latest
        FROM stations st
        LEFT JOIN sensor_stats ss ON ss.station_id = st.station_id
        WHERE st.geometry IS NOT NULL
          AND st.geometry && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
          {where_extra}
        ORDER BY ss.reading_count DESC NULLS LAST
        LIMIT 10000
    """

    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    features = []
    for row in rows:
        lon, lat = row["longitude"], row["latitude"]
        if lon is None or lat is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "station_id":   row["station_id"],
                "name":         row["name"] or row["station_id"],
                "box_type":     row["box_type"],
                "exposure":     row["exposure"],
                "sensor_count": int(row["sensor_count"]),
                "reading_count":int(row["reading_count"]),
                "earliest":     _iso(row["earliest"]),
                "latest":       _iso(row["latest"]),
            },
        })

    return jsonify({
        "type": "FeatureCollection",
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "features": features,
    })


# ---------------------------------------------------------------------------
# 2. Single station detail
# ---------------------------------------------------------------------------

@app.route("/api/station/<station_id>")
def station_detail(station_id):
    """Full info for one station as a GeoJSON Feature."""
    sql = """
        SELECT
            st.station_id,
            st.name,
            st.box_type,
            st.exposure,
            ST_X(st.geometry) AS longitude,
            ST_Y(st.geometry) AS latitude,
            st.created_at
        FROM stations st
        WHERE st.station_id = %s
    """
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (station_id,))
            row = cur.fetchone()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if row is None:
        return jsonify({"error": "Station not found"}), 404

    lon, lat = row["longitude"], row["latitude"]
    return jsonify({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]} if lon else None,
        "properties": {
            "station_id": row["station_id"],
            "name":       row["name"],
            "box_type":   row["box_type"],
            "exposure":   row["exposure"],
            "created_at": _iso(row["created_at"]),
        },
    })


# ---------------------------------------------------------------------------
# 3. Sensors for a station (with availability range + stats)
# ---------------------------------------------------------------------------

@app.route("/api/station/<station_id>/sensors")
def station_sensors(station_id):
    """
    Returns array of sensors with time range of data and aggregate statistics.
    """
    sql = """
        WITH ss AS (
            SELECT
                h.sensor_id,
                SUM(h.reading_count)::BIGINT                                    AS reading_count,
                MIN(h.bucket)                                                   AS earliest,
                MAX(h.bucket) + INTERVAL '1 hour'                               AS latest,
                MIN(h.min_value)                                                AS min_val,
                MAX(h.max_value)                                                AS max_val,
                CASE WHEN SUM(h.reading_count) > 0
                     THEN SUM(h.avg_value * h.reading_count) / SUM(h.reading_count)
                END                                                             AS avg_val
            FROM sensor_data_hourly h
            JOIN sensors s ON s.sensor_id = h.sensor_id
            WHERE s.station_id = %s
            GROUP BY h.sensor_id
        )
        SELECT
            s.sensor_id,
            s.title,
            s.sensor_type,
            s.unit,
            COALESCE(ss.reading_count, 0) AS reading_count,
            ss.earliest,
            ss.latest,
            ss.min_val,
            ss.max_val,
            ss.avg_val
        FROM sensors s
        LEFT JOIN ss ON ss.sensor_id = s.sensor_id
        WHERE s.station_id = %s
        ORDER BY ss.reading_count DESC NULLS LAST
    """
    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (station_id, station_id))
            rows = cur.fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for r in rows:
        result.append({
            "sensor_id":    r["sensor_id"],
            "title":        r["title"],
            "sensor_type":  r["sensor_type"],
            "unit":         r["unit"],
            "reading_count":int(r["reading_count"]),
            "earliest":     _iso(r["earliest"]),
            "latest":       _iso(r["latest"]),
            "min":          round(float(r["min_val"]), 4) if r["min_val"] is not None else None,
            "max":          round(float(r["max_val"]), 4) if r["max_val"] is not None else None,
            "avg":          round(float(r["avg_val"]), 4) if r["avg_val"] is not None else None,
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# 4. Sensor time-series data
# ---------------------------------------------------------------------------

RESOLUTION_TABLES = {
    "raw":     None,                  # special handling
    "hourly":  "sensor_data_hourly",
    "daily":   "sensor_data_daily",
    "monthly": "sensor_data_monthly",
    "yearly":  "sensor_data_yearly",
}


@app.route("/api/sensor/<sensor_id>/data")
def sensor_data(sensor_id):
    """
    ?from=ISO8601  ?to=ISO8601  ?resolution=raw|hourly|daily|monthly|yearly
    """
    resolution = request.args.get("resolution", "hourly").lower()
    if resolution not in RESOLUTION_TABLES:
        return jsonify({"error": f"Invalid resolution. Choose from: {list(RESOLUTION_TABLES)}"}), 400

    dt_from = _parse_dt(request.args.get("from"))
    dt_to   = _parse_dt(request.args.get("to"))

    params = [sensor_id]
    time_filter = ""
    if dt_from:
        time_filter += " AND time_col >= %s"
        params.append(dt_from)
    if dt_to:
        time_filter += " AND time_col <= %s"
        params.append(dt_to)

    try:
        with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # First get sensor metadata
            cur.execute(
                "SELECT title, unit, sensor_type FROM sensors WHERE sensor_id = %s",
                (sensor_id,)
            )
            meta = cur.fetchone()
            if meta is None:
                return jsonify({"error": "Sensor not found"}), 404

            if resolution == "raw":
                sql = f"""
                    SELECT recorded_at AS time_col, value
                    FROM readings
                    WHERE sensor_id = %s
                    {time_filter.replace('time_col', 'recorded_at')}
                    ORDER BY recorded_at
                    LIMIT 100000
                """
                cur.execute(sql, params)
                rows = cur.fetchall()
                data = [{"time": _iso(r["time_col"]), "value": r["value"]} for r in rows]
            else:
                table = RESOLUTION_TABLES[resolution]
                sql = f"""
                    SELECT
                        bucket                   AS time_col,
                        avg_value                AS avg,
                        min_value                AS min,
                        max_value                AS max,
                        reading_count            AS count
                    FROM {table}
                    WHERE sensor_id = %s
                    {time_filter}
                    ORDER BY bucket
                """
                cur.execute(sql, params)
                rows = cur.fetchall()
                data = [
                    {
                        "time":  _iso(r["time_col"]),
                        "avg":   round(float(r["avg"]),  4) if r["avg"]  is not None else None,
                        "min":   round(float(r["min"]),  4) if r["min"]  is not None else None,
                        "max":   round(float(r["max"]),  4) if r["max"]  is not None else None,
                        "count": int(r["count"]),
                    }
                    for r in rows
                ]

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "sensor_id":   sensor_id,
        "title":       meta["title"],
        "unit":        meta["unit"],
        "sensor_type": meta["sensor_type"],
        "resolution":  resolution,
        "record_count":len(data),
        "data":        data,
    })


# ---------------------------------------------------------------------------
# 5. Download helpers
# ---------------------------------------------------------------------------

def _fetch_readings(conn, sensor_ids, dt_from=None, dt_to=None):
    """
    Returns list of dicts: {sensor_id, title, unit, station_id, lon, lat, time, value}
    Uses raw readings table.
    """
    params = list(sensor_ids)
    time_filter = ""
    if dt_from:
        time_filter += " AND r.recorded_at >= %s"
        params.append(dt_from)
    if dt_to:
        time_filter += " AND r.recorded_at <= %s"
        params.append(dt_to)

    placeholders = ",".join(["%s"] * len(sensor_ids))
    sql = f"""
        SELECT
            r.recorded_at,
            r.sensor_id,
            s.title        AS sensor_title,
            s.unit,
            s.station_id,
            ST_X(st.geometry) AS lon,
            ST_Y(st.geometry) AS lat,
            r.value
        FROM readings r
        JOIN sensors s  ON s.sensor_id  = r.sensor_id
        JOIN stations st ON st.station_id = s.station_id
        WHERE r.sensor_id IN ({placeholders})
        {time_filter}
        ORDER BY r.recorded_at, r.sensor_id
        LIMIT 500000
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _to_geojson(rows):
    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [r["lon"], r["lat"]],
            },
            "properties": {
                "station_id":   r["station_id"],
                "sensor_id":    r["sensor_id"],
                "sensor_title": r["sensor_title"],
                "unit":         r["unit"],
                "recorded_at":  r["recorded_at"].isoformat() if r["recorded_at"] else None,
                "value":        r["value"],
            },
        })
    return json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }, ensure_ascii=False).encode("utf-8")


def _to_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "station_id", "sensor_id", "sensor_title", "unit",
        "recorded_at", "value", "lon", "lat",
    ])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "station_id":   r["station_id"],
            "sensor_id":    r["sensor_id"],
            "sensor_title": r["sensor_title"],
            "unit":         r["unit"],
            "recorded_at":  r["recorded_at"].isoformat() if r["recorded_at"] else "",
            "value":        r["value"],
            "lon":          r["lon"],
            "lat":          r["lat"],
        })
    return buf.getvalue().encode("utf-8")


# ── Minimal pure-Python Shapefile writer ────────────────────────────────────
# Writes a Point shapefile with DBF attributes — no shapely/fiona required.

def _pack_shp_record(lon, lat):
    """Pack a single Point geometry record."""
    content = struct.pack("<idd", 1, lon, lat)       # shape type 1 = Point
    rec_len = len(content) // 2                        # length in 16-bit words
    return content, rec_len


def _to_shp_zip(rows):
    """Return bytes of a ZIP containing .shp/.shx/.dbf/.prj for the dataset."""
    # ── DBF ─────────────────────────────────────────────────────────────────
    FIELDS = [
        ("station_id",   "C", 24),
        ("sensor_id",    "C", 24),
        ("sen_title",    "C", 80),
        ("unit",         "C", 20),
        ("recorded_at",  "C", 25),
        ("value",        "N", 19, 6),
        ("lon",          "N", 15, 6),
        ("lat",          "N", 15, 6),
    ]

    def _dbf_bytes(rows, fields):
        num_recs = len(rows)
        header_size = 32 + len(fields) * 32 + 1
        rec_size = 1 + sum(f[2] for f in fields)

        buf = bytearray()
        # Header
        buf += struct.pack("<BBHHHH20x",
            3,                                        # version
            *datetime.date.today().timetuple()[:3],  # yy, mm, dd (will unpack 3 values)
            num_recs,
            header_size,
            rec_size,
        )
        # Field descriptors
        for f in fields:
            name = f[0].encode("ascii").ljust(11, b"\x00")[:11]
            ftype = f[1].encode("ascii")
            flen = f[2]
            fdec = f[3] if len(f) > 3 else 0
            buf += name + ftype + b"\x00" * 4 + bytes([flen, fdec]) + b"\x00" * 14
        buf += b"\r"  # header terminator

        # Records
        for r in rows:
            buf += b" "  # deletion flag
            vals = [
                str(r["station_id"] or "")[:24].ljust(24),
                str(r["sensor_id"]  or "")[:24].ljust(24),
                str(r["sensor_title"] or "")[:80].ljust(80),
                str(r["unit"] or "")[:20].ljust(20),
                (r["recorded_at"].isoformat() if r["recorded_at"] else "")[:25].ljust(25),
                f"{float(r['value'] or 0):19.6f}" if r["value"] is not None else " " * 19,
                f"{float(r['lon']  or 0):15.6f}" if r["lon"]   is not None else " " * 15,
                f"{float(r['lat']  or 0):15.6f}" if r["lat"]   is not None else " " * 15,
            ]
            buf += "".join(vals).encode("ascii", errors="replace")
        buf += b"\x1a"  # EOF
        return bytes(buf)

    # ── SHP + SHX ───────────────────────────────────────────────────────────
    shp_buf = bytearray()
    shx_buf = bytearray()

    def _file_header(file_len_16w, bbox):
        # Big-endian header
        hdr = struct.pack(">IIIIII", 9994, 0, 0, 0, 0, 0)
        hdr += struct.pack(">I", file_len_16w)
        hdr += struct.pack("<II", 1000, 1)          # version, shape type = Point
        hdr += struct.pack("<dddd",
            bbox[0], bbox[1], bbox[2], bbox[3])     # Xmin, Ymin, Xmax, Ymax
        hdr += struct.pack("<dddd", 0.0, 0.0, 0.0, 0.0)  # Zmin/max, Mmin/max
        return hdr

    lons = [r["lon"] for r in rows if r["lon"] is not None]
    lats = [r["lat"] for r in rows if r["lat"] is not None]
    bbox = [
        min(lons) if lons else 0, min(lats) if lats else 0,
        max(lons) if lons else 0, max(lats) if lats else 0,
    ]

    # Placeholder header — we'll prepend it
    records_shp = bytearray()
    records_shx = bytearray()
    shp_offset  = 50  # header = 100 bytes = 50 words

    for i, r in enumerate(rows):
        lon = r["lon"] if r["lon"] is not None else 0.0
        lat = r["lat"] if r["lat"] is not None else 0.0
        content, rec_len = _pack_shp_record(lon, lat)
        rec_header = struct.pack(">II", i + 1, rec_len)  # rec num, content len (words)
        records_shp += rec_header + content
        records_shx += struct.pack(">II", shp_offset, rec_len)
        shp_offset += 4 + rec_len  # 4 words of rec header + content

    shp_len_16w = 50 + len(records_shp) // 2
    shx_len_16w = 50 + len(rows) * 4

    shp_bytes = _file_header(shp_len_16w, bbox) + bytes(records_shp)
    shx_bytes = _file_header(shx_len_16w, bbox) + bytes(records_shx)

    prj = (
        'GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.017453292519943295]]'
    ).encode()

    dbf_bytes = _dbf_bytes(rows, FIELDS)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.shp", shp_bytes)
        zf.writestr("data.shx", shx_bytes)
        zf.writestr("data.dbf", dbf_bytes)
        zf.writestr("data.prj", prj)
    return zip_buf.getvalue()


# ---------------------------------------------------------------------------
# 6. Download — by station
# ---------------------------------------------------------------------------

@app.route("/api/station/<station_id>/download")
def station_download(station_id):
    """
    ?sensor_ids=id1,id2   (optional, default: all)
    ?from=ISO8601          (optional)
    ?to=ISO8601            (optional)
    ?format=geojson|shp|csv
    """
    fmt      = request.args.get("format", "").lower()
    if fmt not in ("geojson", "shp", "csv"):
        return jsonify({"error": "format must be geojson, shp, or csv"}), 400

    raw_ids  = request.args.get("sensor_ids", "")
    dt_from  = _parse_dt(request.args.get("from"))
    dt_to    = _parse_dt(request.args.get("to"))

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if raw_ids:
                    sensor_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
                    cur.execute(
                        "SELECT sensor_id FROM sensors WHERE station_id = %s AND sensor_id = ANY(%s)",
                        (station_id, sensor_ids)
                    )
                else:
                    cur.execute(
                        "SELECT sensor_id FROM sensors WHERE station_id = %s",
                        (station_id,)
                    )
                sensor_ids = [r[0] for r in cur.fetchall()]

            if not sensor_ids:
                return jsonify({"error": "No sensors found for this station"}), 404

            rows = _fetch_readings(conn, sensor_ids, dt_from, dt_to)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return _build_download_response(rows, station_id, fmt)


# ---------------------------------------------------------------------------
# 7. Download — by sensor
# ---------------------------------------------------------------------------

@app.route("/api/sensor/<sensor_id>/download")
def sensor_download(sensor_id):
    """
    ?from=ISO8601   ?to=ISO8601   ?format=geojson|shp|csv
    """
    fmt     = request.args.get("format", "").lower()
    if fmt not in ("geojson", "shp", "csv"):
        return jsonify({"error": "format must be geojson, shp, or csv"}), 400

    dt_from = _parse_dt(request.args.get("from"))
    dt_to   = _parse_dt(request.args.get("to"))

    try:
        with get_conn() as conn:
            rows = _fetch_readings(conn, [sensor_id], dt_from, dt_to)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return _build_download_response(rows, sensor_id, fmt)


def _build_download_response(rows, name, fmt):
    if fmt == "geojson":
        data     = _to_geojson(rows)
        mimetype = "application/geo+json"
        filename = f"{name}.geojson"
    elif fmt == "csv":
        data     = _to_csv(rows)
        mimetype = "text/csv"
        filename = f"{name}.csv"
    else:  # shp
        data     = _to_shp_zip(rows)
        mimetype = "application/zip"
        filename = f"{name}_shp.zip"

    return Response(
        data,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# CORS — allow any origin (public read-only API)
# ---------------------------------------------------------------------------

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


# ---------------------------------------------------------------------------
# Root — API index
# ---------------------------------------------------------------------------

@app.route("/")
def api_index():
    return jsonify({
        "service": "OpenSenseMap Spatial REST API",
        "version": "1.0",
        "endpoints": {
            "stations_in_bbox":    "GET /api/stations?bbox=minLon,minLat,maxLon,maxLat[&exposure=][&box_type=]",
            "station_detail":      "GET /api/station/<station_id>",
            "station_sensors":     "GET /api/station/<station_id>/sensors",
            "sensor_data":         "GET /api/sensor/<sensor_id>/data[?from=&to=&resolution=raw|hourly|daily|monthly|yearly]",
            "station_download":    "GET /api/station/<station_id>/download?format=geojson|shp|csv[&sensor_ids=&from=&to=]",
            "sensor_download":     "GET /api/sensor/<sensor_id>/download?format=geojson|shp|csv[&from=&to=]",
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5052, debug=False)
