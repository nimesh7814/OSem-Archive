"""
openSenseMap Archive – FastAPI backend
======================================
Endpoints
---------
GET  /health
GET  /api/stats
GET  /api/summary
GET  /api/stations
GET  /api/stations/{st_id}
GET  /api/stations/{st_id}/sensors
GET  /api/readings
GET  /api/search
GET  /api/sensors/by-year
POST /api/spatial/bbox          – stations inside a bounding box
POST /api/spatial/radius        – stations within N km of a point
POST /api/spatial/polygon       – stations inside an arbitrary GeoJSON polygon
GET  /api/spatial/readings      – readings for stations in a bbox (query params)
GET  /api/download              – stream CSV for a country / bbox / date range
"""

import csv
import io
import json
import os
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


app = FastAPI(
    title="openSenseMap Archive API",
    description="REST + spatial API for the openSenseMap TimescaleDB archive",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@db:5432/osem",
)



@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()



class BBoxRequest(BaseModel):
    min_lon: float = Field(..., example=6.0,  description="West longitude")
    min_lat: float = Field(..., example=47.0, description="South latitude")
    max_lon: float = Field(..., example=15.0, description="East longitude")
    max_lat: float = Field(..., example=55.0, description="North latitude")
    limit:   int   = Field(500, ge=1, le=5000)
    offset:  int   = Field(0,   ge=0)


class RadiusRequest(BaseModel):
    lon:    float = Field(..., example=7.628,  description="Center longitude")
    lat:    float = Field(..., example=51.962, description="Center latitude")
    radius: float = Field(..., example=50.0,   description="Radius in kilometres")
    limit:  int   = Field(500, ge=1, le=5000)
    offset: int   = Field(0,   ge=0)


class PolygonRequest(BaseModel):
    geojson: dict = Field(
        ...,
        example={
            "type": "Polygon",
            "coordinates": [[[6.0, 47.0], [15.0, 47.0], [15.0, 55.0], [6.0, 55.0], [6.0, 47.0]]],
        },
        description="GeoJSON Polygon or MultiPolygon",
    )
    limit:  int = Field(500, ge=1, le=5000)
    offset: int = Field(0,   ge=0)


class SpatialReadingsRequest(BaseModel):
    min_lon:    float
    min_lat:    float
    max_lon:    float
    max_lat:    float
    time_start: str = Field(..., example="2024-01-01T00:00:00Z")
    time_end:   str = Field(..., example="2024-01-07T23:59:59Z")
    sensor_type: Optional[str] = None
    limit:       int = Field(1000, ge=1, le=10000)



def row_to_dict(record) -> dict:
    return dict(record)


def rows_to_list(records) -> list:
    return [dict(r) for r in records]



@app.get("/health", tags=["Health"])
async def health():
    async with app.state.pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}



@app.get("/api/stats", tags=["Stats"])
async def get_global_stats():
    """Global counts: countries, stations, sensors, readings, downloads."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(DISTINCT country)           AS countries,
                SUM(stations)                     AS stations,
                SUM(sensors)                      AS sensors,
                SUM(readings)                     AS readings
            FROM summary_table
        """)
        dl = await conn.fetchval("SELECT COALESCE(SUM(count),0) FROM downloads")
    return {**dict(row), "downloads": dl}



@app.get("/api/summary", tags=["Stats"])
async def get_summary(
    country: Optional[str] = None,
    region:  Optional[str] = None,
):
    """Return pre-aggregated summary rows, optionally filtered."""
    async with app.state.pool.acquire() as conn:
        if country and region:
            rows = await conn.fetch(
                "SELECT * FROM summary_table WHERE country=$1 AND region=$2 ORDER BY country,region",
                country, region,
            )
        elif country:
            rows = await conn.fetch(
                "SELECT * FROM summary_table WHERE country=$1 ORDER BY region",
                country,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM summary_table ORDER BY country, region"
            )
    return rows_to_list(rows)



@app.get("/api/stations", tags=["Stations"])
async def list_stations(
    country:  Optional[str] = None,
    region:   Optional[str] = None,
    boxtype:  Optional[str] = None,
    exposure: Optional[str] = None,
    limit:    int = Query(100, ge=1, le=2000),
    offset:   int = Query(0,   ge=0),
):
    """List stations with optional filters."""
    conditions = []
    params: list = []

    def add(cond: str, val):
        params.append(val)
        conditions.append(f"{cond} = ${len(params)}")

    if country:  add("country",  country)
    if region:   add("region",   region)
    if boxtype:  add("boxtype",  boxtype)
    if exposure: add("exposure", exposure)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    sql = f"""
        SELECT st_uuid, st_id, boxtype, exposure, model,
               ST_AsGeoJSON(geometry)::json AS geometry,
               region, country, init_date
        FROM stations
        {where}
        ORDER BY st_uuid
        LIMIT ${len(params)-1} OFFSET ${len(params)}
    """
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return rows_to_list(rows)


@app.get("/api/stations/{st_id}", tags=["Stations"])
async def get_station(st_id: str):
    """Get a single station by its string ID."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT st_uuid, st_id, boxtype, exposure, model,
                   ST_AsGeoJSON(geometry)::json AS geometry,
                   region, country, init_date
            FROM stations WHERE st_id=$1
        """, st_id)
    if not row:
        raise HTTPException(404, f"Station '{st_id}' not found")
    return row_to_dict(row)


@app.get("/api/stations/{st_id}/sensors", tags=["Stations"])
async def get_station_sensors(st_id: str):
    """All sensors belonging to a station."""
    async with app.state.pool.acquire() as conn:
        st = await conn.fetchval("SELECT st_uuid FROM stations WHERE st_id=$1", st_id)
        if not st:
            raise HTTPException(404, f"Station '{st_id}' not found")
        rows = await conn.fetch(
            "SELECT * FROM sensors WHERE st_uuid=$1 ORDER BY se_uuid", st
        )
    return rows_to_list(rows)



@app.get("/api/readings", tags=["Readings"])
async def get_readings(
    se_id:      str,
    time_start: str = Query(..., example="2024-01-01T00:00:00Z"),
    time_end:   str = Query(..., example="2024-01-07T23:59:59Z"),
    resolution: str = Query("raw", enum=["raw", "hourly", "daily", "monthly", "yearly"]),
    limit:      int = Query(1000, ge=1, le=50000),
):
    """
    Fetch readings for a sensor in a time range.
    `resolution` selects the continuous aggregate view (raw = hypertable).
    """
    async with app.state.pool.acquire() as conn:
        se_uuid = await conn.fetchval(
            "SELECT se_uuid FROM sensors WHERE se_id=$1", se_id
        )
        if not se_uuid:
            raise HTTPException(404, f"Sensor '{se_id}' not found")

        table_map = {
            "raw":     ("readings",          "time",   "value"),
            "hourly":  ("readings_hourly",   "bucket", "avg_value"),
            "daily":   ("readings_daily",    "bucket", "avg_value"),
            "monthly": ("readings_monthly",  "bucket", "avg_value"),
            "yearly":  ("readings_yearly",   "bucket", "avg_value"),
        }
        tbl, time_col, val_col = table_map[resolution]

        rows = await conn.fetch(f"""
            SELECT {time_col} AS time, {val_col} AS value
            FROM {tbl}
            WHERE se_uuid=$1
              AND {time_col} BETWEEN $2::timestamptz AND $3::timestamptz
            ORDER BY {time_col}
            LIMIT $4
        """, se_uuid, time_start, time_end, limit)

    return {"se_id": se_id, "resolution": resolution, "data": rows_to_list(rows)}



@app.get("/api/search", tags=["Search"])
async def search(
    q:     str = Query(..., min_length=1, description="Country name or sensor type"),
    limit: int = Query(50, ge=1, le=500),
):
    """Full-text search across countries, regions, sensor types, and station IDs."""
    pattern = f"%{q}%"
    async with app.state.pool.acquire() as conn:
        stations = await conn.fetch("""
            SELECT st_uuid, st_id, country, region,
                   ST_AsGeoJSON(geometry)::json AS geometry
            FROM stations
            WHERE country ILIKE $1 OR region ILIKE $1 OR st_id ILIKE $1
            LIMIT $2
        """, pattern, limit)

        sensors = await conn.fetch("""
            SELECT se.se_uuid, se.se_id, se.title, se.type, se.unit,
                   st.country, st.region
            FROM sensors se
            JOIN stations st ON st.st_uuid = se.st_uuid
            WHERE se.type ILIKE $1 OR se.title ILIKE $1
            LIMIT $2
        """, pattern, limit)

    return {
        "stations": rows_to_list(stations),
        "sensors":  rows_to_list(sensors),
    }



@app.get("/api/sensors/by-year", tags=["Stats"])
async def sensors_by_year(
    country: Optional[str] = None,
    region:  Optional[str] = None,
):
    """Sensor registration counts broken down by year, region, country."""
    async with app.state.pool.acquire() as conn:
        if country:
            rows = await conn.fetch("""
                SELECT year, region, country, sensor_count
                FROM sensors_by_year_region_country
                WHERE country=$1
                ORDER BY year DESC
            """, country)
        else:
            rows = await conn.fetch("""
                SELECT year, region, country, sensor_count
                FROM sensors_by_year_region_country
                ORDER BY year DESC, country
            """)
    return rows_to_list(rows)



@app.post("/api/spatial/bbox", tags=["Spatial"])
async def stations_in_bbox(body: BBoxRequest):
    """
    Return all stations whose geometry falls inside the given bounding box.

    Example body:
    ```json
    { "min_lon": 6.0, "min_lat": 47.0, "max_lon": 15.0, "max_lat": 55.0 }
    ```
    """
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT st_uuid, st_id, boxtype, exposure, model,
                   ST_AsGeoJSON(geometry)::json AS geometry,
                   region, country, init_date,
                   ST_X(geometry) AS lon, ST_Y(geometry) AS lat
            FROM stations
            WHERE geometry && ST_MakeEnvelope($1, $2, $3, $4, 4326)
            ORDER BY st_uuid
            LIMIT $5 OFFSET $6
        """, body.min_lon, body.min_lat, body.max_lon, body.max_lat,
             body.limit, body.offset)
    return {
        "type": "FeatureCollection",
        "count": len(rows),
        "features": _to_geojson_features(rows),
    }


@app.post("/api/spatial/radius", tags=["Spatial"])
async def stations_within_radius(body: RadiusRequest):
    """
    Return stations within `radius` kilometres of a point (lon, lat).

    Uses PostGIS `ST_DWithin` on the geography type for accurate km distances.
    """
    radius_m = body.radius * 1000  # convert km → metres
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT st_uuid, st_id, boxtype, exposure, model,
                   ST_AsGeoJSON(geometry)::json AS geometry,
                   region, country, init_date,
                   ST_X(geometry) AS lon, ST_Y(geometry) AS lat,
                   ROUND(
                       ST_Distance(
                           geometry::geography,
                           ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
                       )::numeric / 1000, 2
                   ) AS distance_km
            FROM stations
            WHERE ST_DWithin(
                geometry::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
                $3
            )
            ORDER BY distance_km
            LIMIT $4 OFFSET $5
        """, body.lon, body.lat, radius_m, body.limit, body.offset)
    return {
        "type": "FeatureCollection",
        "center": {"lon": body.lon, "lat": body.lat},
        "radius_km": body.radius,
        "count": len(rows),
        "features": _to_geojson_features(rows),
    }


@app.post("/api/spatial/polygon", tags=["Spatial"])
async def stations_in_polygon(body: PolygonRequest):
    """
    Return stations inside an arbitrary GeoJSON Polygon or MultiPolygon.

    Pass the raw GeoJSON geometry object (not a Feature or FeatureCollection).

    Example body:
    ```json
    {
      "geojson": {
        "type": "Polygon",
        "coordinates": [[[6.0,47.0],[15.0,47.0],[15.0,55.0],[6.0,55.0],[6.0,47.0]]]
      }
    }
    ```
    """
    geojson_str = json.dumps(body.geojson)
    async with app.state.pool.acquire() as conn:
        # Validate geometry type
        geom_type = body.geojson.get("type", "")
        if geom_type not in ("Polygon", "MultiPolygon"):
            raise HTTPException(400, "geojson must be a Polygon or MultiPolygon")
        rows = await conn.fetch("""
            SELECT st_uuid, st_id, boxtype, exposure, model,
                   ST_AsGeoJSON(geometry)::json AS geometry,
                   region, country, init_date,
                   ST_X(geometry) AS lon, ST_Y(geometry) AS lat
            FROM stations
            WHERE ST_Within(
                geometry,
                ST_SetSRID(ST_GeomFromGeoJSON($1), 4326)
            )
            ORDER BY st_uuid
            LIMIT $2 OFFSET $3
        """, geojson_str, body.limit, body.offset)
    return {
        "type": "FeatureCollection",
        "count": len(rows),
        "features": _to_geojson_features(rows),
    }


@app.get("/api/spatial/readings", tags=["Spatial"])
async def spatial_readings(
    min_lon:     float = Query(...),
    min_lat:     float = Query(...),
    max_lon:     float = Query(...),
    max_lat:     float = Query(...),
    time_start:  str   = Query(..., example="2024-01-01T00:00:00Z"),
    time_end:    str   = Query(..., example="2024-01-07T23:59:59Z"),
    sensor_type: Optional[str] = None,
    resolution:  str   = Query("daily", enum=["hourly", "daily", "monthly"]),
    limit:       int   = Query(5000, ge=1, le=50000),
):
    """
    Fetch aggregated readings for **all stations inside a bounding box**
    for a given time range. Useful for heatmaps and regional analysis.
    """
    table_map = {
        "hourly":  ("readings_hourly",  "bucket"),
        "daily":   ("readings_daily",   "bucket"),
        "monthly": ("readings_monthly", "bucket"),
    }
    tbl, time_col = table_map[resolution]

    sensor_filter = "AND se.type ILIKE $7" if sensor_type else ""
    params = [min_lon, min_lat, max_lon, max_lat, time_start, time_end]
    if sensor_type:
        params.append(f"%{sensor_type}%")
    params.append(limit)
    limit_param = f"${len(params)}"

    sql = f"""
        SELECT
            st.st_id, st.country, st.region,
            ST_X(st.geometry) AS lon, ST_Y(st.geometry) AS lat,
            se.se_id, se.type AS sensor_type, se.unit,
            r.{time_col} AS time, r.avg_value AS value
        FROM {tbl} r
        JOIN sensors se ON se.se_uuid = r.se_uuid
        JOIN stations st ON st.st_uuid = se.st_uuid
        WHERE st.geometry && ST_MakeEnvelope($1, $2, $3, $4, 4326)
          AND r.{time_col} BETWEEN $5::timestamptz AND $6::timestamptz
          {sensor_filter}
        ORDER BY r.{time_col}, st.st_id
        LIMIT {limit_param}
    """
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return {
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "resolution": resolution,
        "count": len(rows),
        "data": rows_to_list(rows),
    }



@app.get("/api/download", tags=["Download"])
async def download_csv(
    country:    Optional[str] = None,
    min_lon:    Optional[float] = None,
    min_lat:    Optional[float] = None,
    max_lon:    Optional[float] = None,
    max_lat:    Optional[float] = None,
    time_start: str = Query(..., example="2024-01-01T00:00:00Z"),
    time_end:   str = Query(..., example="2024-01-07T23:59:59Z"),
    sensor_type: Optional[str] = None,
    resolution:  str = Query("daily", enum=["raw", "hourly", "daily", "monthly"]),
):
    """
    Stream a CSV file of readings filtered by country OR bounding box + time range.
    Increments the downloads counter.
    """
    if not country and not all([min_lon, min_lat, max_lon, max_lat]):
        raise HTTPException(400, "Provide either 'country' or all four bbox parameters.")

    table_map = {
        "raw":     ("readings",         "time",   "value"),
        "hourly":  ("readings_hourly",  "bucket", "avg_value"),
        "daily":   ("readings_daily",   "bucket", "avg_value"),
        "monthly": ("readings_monthly", "bucket", "avg_value"),
    }
    tbl, time_col, val_col = table_map[resolution]

    if country:
        geo_filter = "AND st.country = $3"
        params = [time_start, time_end, country]
    else:
        geo_filter = "AND st.geometry && ST_MakeEnvelope($3, $4, $5, $6, 4326)"
        params = [time_start, time_end, min_lon, min_lat, max_lon, max_lat]

    sensor_filter = ""
    if sensor_type:
        params.append(f"%{sensor_type}%")
        sensor_filter = f"AND se.type ILIKE ${len(params)}"

    sql = f"""
        SELECT
            st.st_id, st.country, st.region,
            ST_X(st.geometry) AS lon, ST_Y(st.geometry) AS lat,
            se.se_id, se.type AS sensor_type, se.unit,
            r.{time_col} AS time, r.{val_col} AS value
        FROM {tbl} r
        JOIN sensors se ON se.se_uuid = r.se_uuid
        JOIN stations st ON st.st_uuid = se.st_uuid
        WHERE r.{time_col} BETWEEN $1::timestamptz AND $2::timestamptz
          {geo_filter}
          {sensor_filter}
        ORDER BY r.{time_col}, st.st_id, se.se_id
    """

    async def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["station_id", "country", "region", "lon", "lat",
                         "sensor_id", "sensor_type", "unit", "time", "value"])
        yield output.getvalue()
        output.seek(0); output.truncate()

        async with app.state.pool.acquire() as conn:
            # Increment download counter
            await conn.execute("""
                INSERT INTO downloads (type, count) VALUES ('csv', 1)
                ON CONFLICT DO NOTHING
            """)
            async with conn.transaction():
                async for record in conn.cursor(sql, *params):
                    writer.writerow([
                        record["st_id"], record["country"], record["region"],
                        record["lon"], record["lat"],
                        record["se_id"], record["sensor_type"], record["unit"],
                        record["time"], record["value"],
                    ])
                    yield output.getvalue()
                    output.seek(0); output.truncate()

    filename = f"osem_{country or 'bbox'}_{resolution}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



def _to_geojson_features(rows) -> list:
    features = []
    for r in rows:
        d = dict(r)
        geom = d.pop("geometry", None)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": d,
        })
    return features
