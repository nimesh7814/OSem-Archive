"""
OpenSenseMap — Spatial REST API
Port: 5052

FastAPI rewrite — auto-generated OpenAPI docs at /docs (Swagger UI) and /redoc.
All endpoints are read-only GET requests; no authentication required.
Spatial station queries use the GiST index on stations.geometry.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import os
import struct
import zipfile
from contextlib import contextmanager
from typing import Annotated, Literal, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
from fastapi import FastAPI, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# DB connection pool  (min 2 / max 10 connections — reused across requests)
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",        "localhost"),
    "port":     int(os.getenv("DB_PORT",    "5432")),
    "dbname":   os.getenv("DB_NAME",        "osem_db"),
    "user":     os.getenv("DB_RO_USER",     "web_anonymous"),
    "password": os.getenv("DB_RO_PASSWORD", "osem_readonly"),
}

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=10,
            connect_timeout=10,
            **DB_CONFIG,
        )
    return _pool


@contextmanager
def get_conn():
    """Yield a connection from the pool and return it when done."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.reset()          # discard any implicit transaction state
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# FastAPI app + CORS
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenSenseMap Spatial REST API",
    version="2.1.0",
    description="""
Read-only spatial API over a local [OpenSenseMap](https://opensensemap.org/) archive
stored in **TimescaleDB + PostGIS**.

All station queries use the GiST index on `stations.geometry` for fast bounding-box
and radius lookups. Time-series queries hit pre-computed continuous-aggregate views
(`sensor_data_hourly`, `sensor_data_daily`, `sensor_data_monthly`, `sensor_data_yearly`)
rather than the raw `readings` hypertable — making them orders of magnitude faster at scale.

## Key features
- **GeoJSON** responses for stations and sensor readings
- **Bounding-box** and optional attribute filters for station discovery
- **Multi-resolution** time-series: raw · hourly · daily · monthly · yearly
- **Bulk download** as GeoJSON, CSV, or Shapefile ZIP
- **Pagination** via `limit` / `offset` on all list endpoints
- **No authentication** — all endpoints are public GET requests
""",
    contact={
        "name":  "OpenSenseMap Archive API",
        "url":   "https://opensensemap.org/",
    },
    license_info={
        "name": "Public Domain / ODbL",
        "url":  "https://opendatacommons.org/licenses/odbl/",
    },
    openapi_tags=[
        {"name": "Stations",    "description": "Discover and inspect weather stations"},
        {"name": "Sensors",     "description": "Query sensor metadata and time-series data"},
        {"name": "Downloads",   "description": "Bulk export as GeoJSON · CSV · Shapefile"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------

class PointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float] = Field(
        ..., min_length=2, max_length=2,
        example=[7.6261, 51.9607],
        description="[longitude, latitude] in WGS 84 (EPSG:4326)",
    )


class StationProperties(BaseModel):
    station_id:    str
    name:          Optional[str]
    box_type:      Optional[str]
    exposure:      Optional[str]
    sensor_count:  int
    reading_count: int
    earliest:      Optional[str] = Field(None, description="ISO 8601 UTC timestamp")
    latest:        Optional[str] = Field(None, description="ISO 8601 UTC timestamp")


class StationFeature(BaseModel):
    type:       Literal["Feature"] = "Feature"
    geometry:   Optional[PointGeometry]
    properties: StationProperties


class StationDetailProperties(BaseModel):
    station_id: str
    name:       Optional[str]
    box_type:   Optional[str]
    exposure:   Optional[str]
    created_at: Optional[str]


class StationDetailFeature(BaseModel):
    type:       Literal["Feature"] = "Feature"
    geometry:   Optional[PointGeometry]
    properties: StationDetailProperties


class StationCollection(BaseModel):
    type:     Literal["FeatureCollection"] = "FeatureCollection"
    bbox:     list[float]  = Field(..., description="[minLon, minLat, maxLon, maxLat]")
    count:    int           = Field(..., description="Number of features returned")
    total:    Optional[int] = Field(None, description="Total matching stations (before limit)")
    offset:   int           = Field(0,    description="Offset used in this response")
    features: list[StationFeature]


class SensorSummary(BaseModel):
    sensor_id:     str
    title:         Optional[str]
    sensor_type:   Optional[str]
    unit:          Optional[str]
    reading_count: int
    earliest:      Optional[str]
    latest:        Optional[str]
    min:           Optional[float]
    max:           Optional[float]
    avg:           Optional[float]


class RawDataPoint(BaseModel):
    time:  str
    value: Optional[float]


class AggDataPoint(BaseModel):
    time:  str
    avg:   Optional[float]
    min:   Optional[float]
    max:   Optional[float]
    count: int


class SensorDataResponse(BaseModel):
    sensor_id:    str
    title:        Optional[str]
    unit:         Optional[str]
    sensor_type:  Optional[str]
    resolution:   str
    record_count: int
    data:         list[RawDataPoint] | list[AggDataPoint]


# ---------------------------------------------------------------------------
# Shared query parameters (re-used across endpoints)
# ---------------------------------------------------------------------------

# Common optional station-attribute filters
ExposureQ = Annotated[
    Optional[str],
    Query(
        description=(
            "Filter by exposure type (case-insensitive). "
            "Common values: `outdoor`, `indoor`, `mobile`. "
            "Omit to return all."
        ),
        example="outdoor",
    ),
]

BoxTypeQ = Annotated[
    Optional[str],
    Query(
        description=(
            "Filter by box/device type (case-insensitive). "
            "Common values: `fixed`, `mobile`, `portable`. "
            "Omit to return all."
        ),
        example="fixed",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt):
    return dt.isoformat() if dt else None


def _parse_dt(raw: Optional[str]) -> Optional[datetime.datetime]:
    """
    Accepts any of these formats (all treated as UTC):
      - 2015-01-02 09:00            (space-separated, no seconds → assumed UTC)
      - 2015-01-02 09:00:00         (space-separated, with seconds → assumed UTC)
      - 2015-01-02T09:00:00Z        (ISO 8601 with Z)
      - 2015-01-02T09:00:00+00:00   (ISO 8601 with offset)
    """
    if not raw:
        return None
    normalised = raw.strip().replace(" ", "T").replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(normalised)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


RESOLUTION_TABLES = {
    "raw":     None,
    "hourly":  "sensor_data_hourly",
    "daily":   "sensor_data_daily",
    "monthly": "sensor_data_monthly",
    "yearly":  "sensor_data_yearly",
}


def _build_attr_filters(
    exposure: Optional[str],
    box_type: Optional[str],
    table_alias: str = "st",
) -> tuple[list[str], list]:
    """
    Returns (filter_clauses, params) for optional exposure / box_type filters.
    Both are case-insensitive. Silently ignores blank strings.
    """
    clauses: list[str] = []
    params:  list      = []

    if exposure and exposure.strip():
        clauses.append(f"LOWER({table_alias}.exposure) = %s")
        params.append(exposure.strip().lower())
    if box_type and box_type.strip():
        clauses.append(f"LOWER({table_alias}.box_type) = %s")
        params.append(box_type.strip().lower())

    return clauses, params


# Shared CTE that aggregates per-station stats from the hourly rollup view.
# Injected into every station-list query so the pattern stays consistent.
_SENSOR_STATS_CTE = """
    sensor_stats AS (
        SELECT
            s.station_id,
            COUNT(DISTINCT s.sensor_id)               AS sensor_count,
            COALESCE(SUM(h.reading_count), 0)::BIGINT  AS reading_count,
            MIN(h.bucket)                              AS earliest,
            MAX(h.bucket) + INTERVAL '1 hour'          AS latest
        FROM sensors s
        LEFT JOIN sensor_data_hourly h ON h.sensor_id = s.sensor_id
        GROUP BY s.station_id
    )
"""

_STATION_SELECT_COLS = """
    st.station_id,
    st.name,
    st.box_type,
    st.exposure,
    ST_X(st.geometry) AS longitude,
    ST_Y(st.geometry) AS latitude,
    COALESCE(ss.sensor_count,  0) AS sensor_count,
    COALESCE(ss.reading_count, 0) AS reading_count,
    ss.earliest,
    ss.latest
"""


def _rows_to_features(rows) -> list[StationFeature]:
    features = []
    for row in rows:
        lon, lat = row["longitude"], row["latitude"]
        if lon is None or lat is None:
            continue
        features.append(StationFeature(
            geometry=PointGeometry(coordinates=[lon, lat]),
            properties=StationProperties(
                station_id=row["station_id"],
                name=row["name"] or row["station_id"],
                box_type=row["box_type"],
                exposure=row["exposure"],
                sensor_count=int(row["sensor_count"]),
                reading_count=int(row["reading_count"]),
                earliest=_iso(row["earliest"]),
                latest=_iso(row["latest"]),
            ),
        ))
    return features


# ---------------------------------------------------------------------------
# 1. GET /api/stations  — bounding-box station query (GiST index)
# ---------------------------------------------------------------------------

@app.get(
    "/api/stations",
    response_model=StationCollection,
    tags=["Stations"],
    summary="List stations inside a bounding box",
    description="""
Returns a **GeoJSON FeatureCollection** of stations whose GPS coordinates fall
inside the given bounding box.

The spatial filter uses PostGIS `&&` against the **GiST index** on
`stations.geometry`. Statistics are pre-aggregated from `sensor_data_hourly`.

Results are ordered by `reading_count` descending.
Use `limit` (default **20**, max **10 000**) and `offset` to paginate.
`exposure` and `box_type` are **optional** — omit to return all station types.
""",
)
def stations_in_bbox(
    bbox: Annotated[str, Query(
        description="Bounding box as `minLon,minLat,maxLon,maxLat` (WGS 84)",
        example="7.0,51.5,8.5,52.5",
    )],
    exposure: ExposureQ = None,
    box_type:  BoxTypeQ  = None,
    limit: Annotated[int, Query(
        ge=1, le=10_000,
        description="Maximum number of stations to return (default 20)",
    )] = 20,
    offset: Annotated[int, Query(
        ge=0,
        description="Number of stations to skip for pagination (default 0)",
    )] = 0,
):
    try:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="bbox must be four comma-separated floats: minLon,minLat,maxLon,maxLat",
        )

    min_lon, min_lat, max_lon, max_lat = parts
    attr_clauses, attr_params = _build_attr_filters(exposure, box_type)
    where_extra = ("AND " + " AND ".join(attr_clauses)) if attr_clauses else ""

    # Spatial params come before attribute params; LIMIT/OFFSET at the end.
    base_params: list = [min_lon, min_lat, max_lon, max_lat]
    count_params = base_params + attr_params
    data_params  = base_params + attr_params + [limit, offset]

    sql_base = f"""
        WITH {_SENSOR_STATS_CTE}
        SELECT {_STATION_SELECT_COLS}
        FROM stations st
        LEFT JOIN sensor_stats ss ON ss.station_id = st.station_id
        WHERE st.geometry IS NOT NULL
          AND st.geometry && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
          {where_extra}
    """

    sql_count = f"SELECT COUNT(*) FROM ({sql_base}) _sub"
    sql_data  = f"{sql_base} ORDER BY ss.reading_count DESC NULLS LAST LIMIT %s OFFSET %s"

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql_count, count_params)
                total = cur.fetchone()[0]
                cur.execute(sql_data, data_params)
                rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return StationCollection(
        bbox=[min_lon, min_lat, max_lon, max_lat],
        count=len(rows),
        total=total,
        offset=offset,
        features=_rows_to_features(rows),
    )


# ---------------------------------------------------------------------------
# 2. GET /api/stations/near  — radius search (GiST index via ST_DWithin)
# ---------------------------------------------------------------------------

@app.get(
    "/api/stations/near",
    response_model=StationCollection,
    tags=["Stations"],
    summary="List stations within a radius",
    description="""
Returns stations within `radius_km` kilometres of the given point, ordered by
distance ascending.

Uses PostGIS `ST_DWithin` on the **geography** cast of `stations.geometry` for
accurate great-circle distance. The GiST index still handles the initial
bounding-box pre-filter.

Use `limit` (default **20**, max **10 000**) and `offset` to paginate.
`exposure` and `box_type` are **optional** — omit to return all station types.
""",
)
def stations_near(
    lon: Annotated[float, Query(description="Longitude of centre point (WGS 84)", example=7.6261)],
    lat: Annotated[float, Query(description="Latitude of centre point (WGS 84)", example=51.9607)],
    radius_km: Annotated[float, Query(gt=0, le=500, description="Search radius in kilometres", example=20)],
    exposure: ExposureQ = None,
    box_type:  BoxTypeQ  = None,
    limit:  Annotated[int, Query(ge=1, le=10_000, description="Max stations to return (default 20)")] = 20,
    offset: Annotated[int, Query(ge=0,             description="Pagination offset (default 0)")]      = 0,
):
    radius_m = radius_km * 1000
    attr_clauses, attr_params = _build_attr_filters(exposure, box_type)
    where_extra = ("AND " + " AND ".join(attr_clauses)) if attr_clauses else ""

    # lon/lat needed twice: once for ST_Distance, once for ST_DWithin
    geo_params: list = [lon, lat, lon, lat, radius_m]
    count_params = geo_params + attr_params
    data_params  = geo_params + attr_params + [limit, offset]

    sql_base = f"""
        WITH {_SENSOR_STATS_CTE}
        SELECT
            {_STATION_SELECT_COLS},
            ROUND((ST_Distance(
                st.geometry::geography,
                ST_MakePoint(%s, %s)::geography
            ) / 1000)::numeric, 3) AS distance_km
        FROM stations st
        LEFT JOIN sensor_stats ss ON ss.station_id = st.station_id
        WHERE st.geometry IS NOT NULL
          AND ST_DWithin(
                st.geometry::geography,
                ST_MakePoint(%s, %s)::geography,
                %s
              )
          {where_extra}
    """

    sql_count = f"SELECT COUNT(*) FROM ({sql_base}) _sub"
    sql_data  = f"{sql_base} ORDER BY distance_km ASC LIMIT %s OFFSET %s"

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql_count, count_params)
                total = cur.fetchone()[0]
                cur.execute(sql_data, data_params)
                rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    lons = [r["longitude"] for r in rows if r["longitude"] is not None]
    lats = [r["latitude"]  for r in rows if r["latitude"]  is not None]
    result_bbox = [
        min(lons, default=lon), min(lats, default=lat),
        max(lons, default=lon), max(lats, default=lat),
    ]

    return StationCollection(
        bbox=result_bbox,
        count=len(rows),
        total=total,
        offset=offset,
        features=_rows_to_features(rows),
    )


# ---------------------------------------------------------------------------
# 3. GET /api/station/{station_id}  — single station detail
# ---------------------------------------------------------------------------

@app.get(
    "/api/station/{station_id}",
    response_model=StationDetailFeature,
    tags=["Stations"],
    summary="Get a single station by ID",
    description="Returns full metadata for one station as a GeoJSON Feature.",
    responses={404: {"description": "Station not found"}},
)
def station_detail(
    station_id: Annotated[str, Path(
        description="24-character hex OpenSenseMap station ID",
        example="5f7b1e2d3a4c5e6f7b8c9d0e",
        min_length=24, max_length=24,
    )],
):
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
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (station_id,))
                row = cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if row is None:
        raise HTTPException(status_code=404, detail="Station not found")

    lon, lat = row["longitude"], row["latitude"]
    return StationDetailFeature(
        geometry=PointGeometry(coordinates=[lon, lat]) if lon is not None else None,
        properties=StationDetailProperties(
            station_id=row["station_id"],
            name=row["name"],
            box_type=row["box_type"],
            exposure=row["exposure"],
            created_at=_iso(row["created_at"]),
        ),
    )


# ---------------------------------------------------------------------------
# 4. GET /api/station/{station_id}/sensors
# ---------------------------------------------------------------------------

@app.get(
    "/api/station/{station_id}/sensors",
    response_model=list[SensorSummary],
    tags=["Stations"],
    summary="List sensors for a station",
    description="""
Returns all sensors belonging to the station, ordered by total `reading_count`
descending. Stats (`min`, `avg`, `max`) come from `sensor_data_hourly`.
""",
    responses={404: {"description": "Station not found"}},
)
def station_sensors(
    station_id: Annotated[str, Path(
        description="24-character hex station ID",
        min_length=24, max_length=24,
    )],
):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Single round-trip: existence check + sensor list in one query.
                # If no rows come back we distinguish "station not found" vs
                # "station exists but has no sensors" by a second cheap query.
                cur.execute("""
                    WITH ss AS (
                        SELECT
                            h.sensor_id,
                            SUM(h.reading_count)::BIGINT                              AS reading_count,
                            MIN(h.bucket)                                             AS earliest,
                            MAX(h.bucket) + INTERVAL '1 hour'                        AS latest,
                            MIN(h.min_value)                                          AS min_val,
                            MAX(h.max_value)                                          AS max_val,
                            CASE WHEN SUM(h.reading_count) > 0
                                 THEN SUM(h.avg_value * h.reading_count)
                                      / SUM(h.reading_count)
                            END                                                       AS avg_val
                        FROM sensor_data_hourly h
                        JOIN sensors s ON s.sensor_id = h.sensor_id
                        WHERE s.station_id = %s
                        GROUP BY h.sensor_id
                    )
                    SELECT
                        s.sensor_id, s.title, s.sensor_type, s.unit,
                        COALESCE(ss.reading_count, 0) AS reading_count,
                        ss.earliest, ss.latest,
                        ss.min_val, ss.max_val, ss.avg_val
                    FROM sensors s
                    LEFT JOIN ss ON ss.sensor_id = s.sensor_id
                    WHERE s.station_id = %s
                    ORDER BY ss.reading_count DESC NULLS LAST
                """, (station_id, station_id))
                rows = cur.fetchall()

                if not rows:
                    cur.execute("SELECT 1 FROM stations WHERE station_id = %s", (station_id,))
                    if cur.fetchone() is None:
                        raise HTTPException(status_code=404, detail="Station not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return [
        SensorSummary(
            sensor_id=r[0],
            title=r[1],
            sensor_type=r[2],
            unit=r[3],
            reading_count=int(r[4]),
            earliest=_iso(r[5]),
            latest=_iso(r[6]),
            min=round(float(r[7]), 4) if r[7] is not None else None,
            max=round(float(r[8]), 4) if r[8] is not None else None,
            avg=round(float(r[9]), 4) if r[9] is not None else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 5. GET /api/sensor/{sensor_id}/data  — time-series
# ---------------------------------------------------------------------------

@app.get(
    "/api/sensor/{sensor_id}/data",
    tags=["Sensors"],
    summary="Get time-series data for a sensor",
    description="""
Returns time-series readings at the requested resolution.

| Resolution | Source table | Typical speed |
|---|---|---|
| `raw` | `readings` hypertable | Slow for large ranges |
| `hourly` | `sensor_data_hourly` | Fast |
| `daily` | `sensor_data_daily` | Very fast |
| `monthly` | `sensor_data_monthly` | Instant |
| `yearly` | `sensor_data_yearly` | Instant |

`raw` results are capped at **100 000 rows** and support `limit`/`offset`.
All other resolutions return every bucket in the requested range (no cap).

**Tip:** omit `from` / `to` entirely to get all available data — the query
hits a pre-computed view and is very fast at any resolution above `raw`.
""",
    responses={
        404: {"description": "Sensor not found"},
        400: {"description": "Invalid resolution or date format"},
    },
)
def sensor_data(
    sensor_id: Annotated[str, Path(
        description="24-character hex sensor ID",
        min_length=24, max_length=24,
    )],
    from_: Annotated[Optional[str], Query(
        alias="from",
        description="Start of time range (ISO 8601). Omit for all available data.",
        example="2015-01-02 09:00",
    )] = None,
    to: Annotated[Optional[str], Query(
        description="End of time range (ISO 8601). Omit for all available data.",
        example="2023-01-02 23:00",
    )] = None,
    resolution: Annotated[
        Literal["raw", "hourly", "daily", "monthly", "yearly"],
        Query(description="Time-series granularity (default: hourly)"),
    ] = "hourly",
    limit: Annotated[Optional[int], Query(
        ge=1, le=100_000,
        description="Max rows to return — only applies to `raw` resolution (default 10 000)",
    )] = None,
    offset: Annotated[int, Query(
        ge=0,
        description="Pagination offset — only applies to `raw` resolution (default 0)",
    )] = 0,
):
    dt_from = _parse_dt(from_)
    dt_to   = _parse_dt(to)

    # Default limit for raw: 10 000 if caller didn't specify
    raw_limit  = limit if limit is not None else 10_000
    raw_offset = offset

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT title, unit, sensor_type FROM sensors WHERE sensor_id = %s",
                    (sensor_id,),
                )
                meta = cur.fetchone()
                if meta is None:
                    raise HTTPException(status_code=404, detail="Sensor not found")

                params: list = [sensor_id]
                time_filter = ""
                if dt_from:
                    time_filter += " AND time_col >= %s"
                    params.append(dt_from)
                if dt_to:
                    time_filter += " AND time_col <= %s"
                    params.append(dt_to)

                if resolution == "raw":
                    sql = f"""
                        SELECT recorded_at AS time_col, value
                        FROM readings
                        WHERE sensor_id = %s
                        {time_filter.replace('time_col', 'recorded_at')}
                        ORDER BY recorded_at
                        LIMIT %s OFFSET %s
                    """
                    cur.execute(sql, params + [raw_limit, raw_offset])
                    data = [
                        {"time": _iso(r["time_col"]), "value": r["value"]}
                        for r in cur.fetchall()
                    ]
                else:
                    table = RESOLUTION_TABLES[resolution]
                    sql = f"""
                        SELECT
                            bucket        AS time_col,
                            avg_value     AS avg,
                            min_value     AS min,
                            max_value     AS max,
                            reading_count AS count
                        FROM {table}
                        WHERE sensor_id = %s
                        {time_filter}
                        ORDER BY bucket
                    """
                    cur.execute(sql, params)
                    data = [
                        {
                            "time":  _iso(r["time_col"]),
                            "avg":   round(float(r["avg"]),  4) if r["avg"]  is not None else None,
                            "min":   round(float(r["min"]),  4) if r["min"]  is not None else None,
                            "max":   round(float(r["max"]),  4) if r["max"]  is not None else None,
                            "count": int(r["count"]),
                        }
                        for r in cur.fetchall()
                    ]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "sensor_id":    sensor_id,
        "title":        meta["title"],
        "unit":         meta["unit"],
        "sensor_type":  meta["sensor_type"],
        "resolution":   resolution,
        "record_count": len(data),
        "data":         data,
    }


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _fetch_readings(conn, sensor_ids, dt_from=None, dt_to=None):
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
        JOIN sensors  s  ON s.sensor_id   = r.sensor_id
        JOIN stations st ON st.station_id = s.station_id
        WHERE r.sensor_id IN ({placeholders})
        {time_filter}
        ORDER BY r.recorded_at, r.sensor_id
        LIMIT 500000
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _to_geojson(rows: list[dict]) -> bytes:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "station_id":   r["station_id"],
                "sensor_id":    r["sensor_id"],
                "sensor_title": r["sensor_title"],
                "unit":         r["unit"],
                "recorded_at":  r["recorded_at"].isoformat() if r["recorded_at"] else None,
                "value":        r["value"],
            },
        }
        for r in rows
    ]
    return json.dumps({"type": "FeatureCollection", "features": features},
                      ensure_ascii=False).encode("utf-8")


def _to_csv(rows: list[dict]) -> bytes:
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


def _pack_shp_record(lon, lat):
    content = struct.pack("<idd", 1, lon, lat)
    return content, len(content) // 2


def _to_shp_zip(rows: list[dict]) -> bytes:
    FIELDS = [
        ("station_id",  "C", 24),
        ("sensor_id",   "C", 24),
        ("sen_title",   "C", 80),
        ("unit",        "C", 20),
        ("recorded_at", "C", 25),
        ("value",       "N", 19, 6),
        ("lon",         "N", 15, 6),
        ("lat",         "N", 15, 6),
    ]

    def _dbf_bytes(rows, fields):
        num_recs = len(rows)
        header_size = 32 + len(fields) * 32 + 1
        rec_size = 1 + sum(f[2] for f in fields)
        buf = bytearray()
        today = datetime.date.today()
        buf += struct.pack(
            "<BBBBIHh20x",
            3,                  # dBASE III version
            today.year % 100,   # last-update YY
            today.month,        # last-update MM
            today.day,          # last-update DD
            num_recs,
            header_size,
            rec_size,
        )
        for f in fields:
            name = f[0].encode("ascii").ljust(11, b"\x00")[:11]
            buf += name + f[1].encode("ascii") + b"\x00" * 4 + bytes([f[2], f[3] if len(f) > 3 else 0]) + b"\x00" * 14
        buf += b"\r"
        for r in rows:
            buf += b" "
            vals = [
                str(r["station_id"] or "")[:24].ljust(24),
                str(r["sensor_id"]  or "")[:24].ljust(24),
                str(r["sensor_title"] or "")[:80].ljust(80),
                str(r["unit"] or "")[:20].ljust(20),
                (r["recorded_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if r["recorded_at"] else "")[:25].ljust(25),
                f"{float(r['value'] or 0):19.6f}" if r["value"] is not None else " " * 19,
                f"{float(r['lon']   or 0):15.6f}" if r["lon"]   is not None else " " * 15,
                f"{float(r['lat']   or 0):15.6f}" if r["lat"]   is not None else " " * 15,
            ]
            buf += "".join(vals).encode("ascii", errors="replace")
        buf += b"\x1a"
        return bytes(buf)

    def _file_header(file_len, bbox):
        hdr = struct.pack(">IIIIII", 9994, 0, 0, 0, 0, 0)
        hdr += struct.pack(">I", file_len)
        hdr += struct.pack("<II", 1000, 1)
        hdr += struct.pack("<dddd", *bbox)
        hdr += struct.pack("<dddd", 0.0, 0.0, 0.0, 0.0)
        return hdr

    lons = [r["lon"] for r in rows if r["lon"] is not None]
    lats = [r["lat"] for r in rows if r["lat"] is not None]
    bbox = [min(lons, default=0), min(lats, default=0),
            max(lons, default=0), max(lats, default=0)]

    records_shp = bytearray()
    records_shx = bytearray()
    shp_offset = 50

    for i, r in enumerate(rows):
        lon = r["lon"] if r["lon"] is not None else 0.0
        lat = r["lat"] if r["lat"] is not None else 0.0
        content, rec_len = _pack_shp_record(lon, lat)
        records_shp += struct.pack(">II", i + 1, rec_len) + content
        records_shx += struct.pack(">II", shp_offset, rec_len)
        shp_offset += 4 + rec_len

    shp_bytes = _file_header(50 + len(records_shp) // 2, bbox) + bytes(records_shp)
    shx_bytes = _file_header(50 + len(rows) * 4,         bbox) + bytes(records_shx)
    prj = (
        'GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.017453292519943295]]'
    ).encode()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.shp", shp_bytes)
        zf.writestr("data.shx", shx_bytes)
        zf.writestr("data.dbf", _dbf_bytes(rows, FIELDS))
        zf.writestr("data.prj", prj)
    return zip_buf.getvalue()


def _build_download_response(rows: list[dict], name: str, fmt: str) -> Response:
    if fmt == "geojson":
        return Response(
            content=_to_geojson(rows),
            media_type="application/geo+json",
            headers={"Content-Disposition": f'attachment; filename="{name}.geojson"'},
        )
    if fmt == "csv":
        return Response(
            content=_to_csv(rows),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    return Response(
        content=_to_shp_zip(rows),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}_shp.zip"'},
    )


# ---------------------------------------------------------------------------
# 6. GET /api/station/{station_id}/download
# ---------------------------------------------------------------------------

@app.get(
    "/api/station/{station_id}/download",
    tags=["Downloads"],
    summary="Download all readings for a station",
    description="""
Bulk-exports raw readings for one station (or a subset of its sensors) as
**GeoJSON**, **CSV**, or **Shapefile ZIP**.

Results are capped at **500 000 rows**. Narrow the range with `from` / `to`
if you need more control.
""",
    responses={
        400: {"description": "Invalid format"},
        404: {"description": "No sensors found"},
    },
)
def station_download(
    station_id: Annotated[str, Path(min_length=24, max_length=24)],
    format: Annotated[Literal["geojson", "csv", "shp"], Query(
        description="Output format",
        example="csv",
    )],
    sensor_ids: Annotated[Optional[str], Query(
        description="Comma-separated sensor IDs to include (default: all sensors)",
        example="5f7b1e2d3a4c5e6f7b8c9d0e,5f7b1e2d3a4c5e6f7b8c9d0f",
    )] = None,
    from_: Annotated[Optional[str], Query(
        alias="from",
        description="Start of time range (ISO 8601)",
        example="2015-01-02 09:00",
    )] = None,
    to: Annotated[Optional[str], Query(
        description="End of time range (ISO 8601)",
        example="2023-01-02 23:00",
    )] = None,
):
    dt_from = _parse_dt(from_)
    dt_to   = _parse_dt(to)

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if sensor_ids:
                    ids = [s.strip() for s in sensor_ids.split(",") if s.strip()]
                    cur.execute(
                        "SELECT sensor_id FROM sensors WHERE station_id = %s AND sensor_id = ANY(%s)",
                        (station_id, ids),
                    )
                else:
                    cur.execute("SELECT sensor_id FROM sensors WHERE station_id = %s", (station_id,))
                ids = [r[0] for r in cur.fetchall()]

            if not ids:
                raise HTTPException(status_code=404, detail="No sensors found for this station")

            rows = _fetch_readings(conn, ids, dt_from, dt_to)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        return _build_download_response(rows, station_id, format)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build response: {e}")


# ---------------------------------------------------------------------------
# 7. GET /api/sensor/{sensor_id}/download
# ---------------------------------------------------------------------------

@app.get(
    "/api/sensor/{sensor_id}/download",
    tags=["Downloads"],
    summary="Download all readings for a sensor",
    description="Bulk-exports raw readings for a single sensor. Results capped at **500 000 rows**.",
    responses={
        400: {"description": "Invalid format"},
        404: {"description": "Sensor not found"},
    },
)
def sensor_download(
    sensor_id: Annotated[str, Path(min_length=24, max_length=24)],
    format: Annotated[Literal["geojson", "csv", "shp"], Query(
        description="Output format", example="geojson",
    )],
    from_: Annotated[Optional[str], Query(
        alias="from",
        description="Start of time range (ISO 8601)",
        example="2015-01-02 09:00",
    )] = None,
    to: Annotated[Optional[str], Query(
        description="End of time range (ISO 8601)",
        example="2023-01-02 23:00",
    )] = None,
):
    dt_from = _parse_dt(from_)
    dt_to   = _parse_dt(to)

    try:
        with get_conn() as conn:
            rows = _fetch_readings(conn, [sensor_id], dt_from, dt_to)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not rows:
        raise HTTPException(status_code=404, detail="Sensor not found or no readings in range")

    try:
        return _build_download_response(rows, sensor_id, format)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to build response: {e}")


# ---------------------------------------------------------------------------
# Root — redirect to docs
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=5052, reload=False)
