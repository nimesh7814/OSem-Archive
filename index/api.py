from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from typing import List, Optional
from datetime import date, datetime
from urllib.parse import unquote
from utils import build_output_file, OutputType
import json
import psycopg2
import psycopg2.pool
import os
import math
import time
import threading
 
load_dotenv()


_pool: psycopg2.pool.ThreadedConnectionPool = None
 
def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=5,
            maxconn=int(os.getenv("DB_POOL_MAX", 40)),
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", 5436)),
            database=os.getenv("POSTGRES_DB"),
            user=os.getenv("POSTGRES_USER"),
            password=os.getenv("POSTGRES_PASSWORD"),
        )
    return _pool
 
def get_db_connection():
    """Borrow a connection from the pool."""
    return get_pool().getconn()
 
def release_conn(conn):
    """Return connection to the pool (even on error)."""
    get_pool().putconn(conn)

class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._store: dict = {}
        self._lock = threading.Lock()
 
    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry and (time.monotonic() - entry["ts"]) < self.ttl:
                return entry["val"]
        return None
 
    def set(self, key: str, val):
        with self._lock:
            self._store[key] = {"val": val, "ts": time.monotonic()}


# Cache TTLs
_summary_cache = TTLCache(ttl_seconds=60)
_countries_cache = TTLCache(ttl_seconds=300)
_regions_cache = TTLCache(ttl_seconds=300)
_categories_cache = TTLCache(ttl_seconds=300)
_stations_cache = TTLCache(ttl_seconds=120)
_country_region_cache = TTLCache(ttl_seconds=600)

app = FastAPI(title="OpenSenseMap Index API")
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _run_query(sql: str, params=None):
    """Borrow conn, run query, release conn, return rows."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        release_conn(conn)
 
def _run_query_one(sql: str, params=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return row
    finally:
        release_conn(conn)

def _split_csv_filter(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]

# Health check endpoint
@app.get("/")
def status():
    return {"message": "OK!"}

# Summary endpoint with caching
@app.get("/summary")
def get_summary():
    cached = _summary_cache.get("summary")
    if cached:
        return cached
 
    row = _run_query_one("""
        SELECT stations, sensors, readings, countries, regions, last_updated
        FROM summary
        WHERE id = 1
    """)
 
    if not row:
        raise HTTPException(status_code=503, detail="Summary not yet computed. Run SELECT refresh_summary() in Postgres.")
 
    result = {
        "total_stations": row[0],
        "total_sensors": row[1],
        "total_readings": row[2],
        "total_countries": row[3],
        "total_regions": row[4],
        "last_updated": row[5].isoformat() if row[5] else None,
    }
    _summary_cache.set("summary", result)
    return result

# Get the list of countries
@app.get("/countries")
def get_countries():
    cached = _countries_cache.get("countries")
    if cached:
        return cached
 
    rows = _run_query("SELECT DISTINCT country FROM stations WHERE country IS NOT NULL ORDER BY country")
    result = {"countries": [r[0] for r in rows]}
    _countries_cache.set("countries", result)
    return result

# Get the list of regions
@app.get("/regions")
def get_regions(country: str):
    cache_key = f"regions:{country}"
    cached = _regions_cache.get(cache_key)
    if cached:
        return cached
 
    rows = _run_query(
        "SELECT DISTINCT region FROM stations WHERE country = %s AND region IS NOT NULL ORDER BY region",
        (country,)
    )
    result = {"regions": [r[0] for r in rows]}
    _regions_cache.set(cache_key, result)
    return result

# Get the list of sensor categories
@app.get("/sensor_categories")
def get_sensor_categories():
    cached = _categories_cache.get("categories")
    if cached:
        return cached
 
    rows = _run_query("SELECT DISTINCT category FROM sensors WHERE category IS NOT NULL ORDER BY category")
    result = {"categories": [r[0] for r in rows]}
    _categories_cache.set("categories", result)
    return result

# Get station markers and Station id given aggregated measurements given
@app.get("/get_stations")
def get_stations(
    station_id: Optional[str] = Query(None, description="Optional station ID"),
    limit: int = Query(5000, le=10000, description="Max markers to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    if station_id:
        row = _run_query_one('''
            SELECT
                st.st_id,
                st.name,
                st.country,
                st.region,
                ST_Y(st.location::geometry) AS latitude,
                ST_X(st.location::geometry) AS longitude,
                COUNT(DISTINCT se.se_id) AS total_sensors,
                SUM(re.count) AS total_readings,
                STRING_AGG(DISTINCT se.category, ', ') AS categories
            FROM stations st
            JOIN sensors se ON st.st_id = se.st_id
            JOIN readings re ON se.se_id = re.se_id
            WHERE st.st_id = %s
            GROUP BY st.st_id, st.name, st.country, st.region, st.location
        ''', (station_id,))
 
        if not row:
            return []
        return {
            "st_id": row[0], 
            "name": row[1], 
            "country": row[2], 
            "region": row[3],
            "latitude": row[4], 
            "longitude": row[5],
            "total_sensors": row[6], 
            "total_readings": row[7], 
            "categories": row[8],
        }
 
    # All stations cached per page
    cache_key = f"stations:{limit}:{offset}"
    cached = _stations_cache.get(cache_key)
    if cached:
        return cached
 
    rows = _run_query('''
        SELECT
            st_id,
            name,
            ST_Y(location::geometry) AS latitude,
            ST_X(location::geometry) AS longitude
        FROM stations
        WHERE location IS NOT NULL
        ORDER BY st_id
        LIMIT %s OFFSET %s
    ''', (limit, offset))
 
    result = [{"st_id": r[0], "name": r[1], "latitude": r[2], "longitude": r[3]} for r in rows]
    _stations_cache.set(cache_key, result)
    return result


# Get station metadata, daily chart data
@app.get("/station_readings")
def station_readings(
    st_id: str = Query(..., description="Station ID"),
    month: int = Query(..., description="Month as integer e.g. 5"),
    year:  int = Query(..., description="Year as integer e.g. 2026"),
):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
 
        cur.execute("""
            SELECT
                st.st_id,
                st.name,
                ST_Y(st.location::geometry) AS latitude,
                ST_X(st.location::geometry) AS longitude,
                COUNT(DISTINCT se.se_id) AS total_sensors,
                SUM(re.count) AS total_readings,
                STRING_AGG(DISTINCT se.category, ', ') AS categories,
                st.country,
                st.region
            FROM stations st
            JOIN sensors  se ON st.st_id = se.st_id
            JOIN readings re ON se.se_id = re.se_id
            WHERE st.st_id = %s
            GROUP BY st.st_id, st.name, st.location, st.country, st.region;
        """, (st_id,))
 
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Station '{st_id}' not found.")
 
        station = {
            "st_id": row[0], "name": row[1],
            "latitude": float(row[2]) if row[2] is not None else None,
            "longitude": float(row[3]) if row[3] is not None else None,
            "total_sensors": row[4],
            "total_readings": int(row[5]) if row[5] is not None else 0,
            "categories": row[6], "country": row[7], "region": row[8],
        }
 
        cur.execute("""
            SELECT
                EXTRACT(DAY FROM re.date)::int AS day,
                se.category,
                se.unit,
                se.title,
                ROUND(AVG(re.avg_value)::numeric, 2) AS avg_value,
                ROUND(AVG(re.min_value)::numeric, 2) AS min_value,
                ROUND(AVG(re.max_value)::numeric, 2) AS max_value
            FROM readings re
            JOIN sensors se ON re.se_id = se.se_id
            WHERE se.st_id = %s
              AND EXTRACT(MONTH FROM re.date) = %s
              AND EXTRACT(YEAR  FROM re.date) = %s
              AND se.category IS NOT NULL
            GROUP BY day, se.category, se.unit, se.title
            ORDER BY day, se.category;
        """, (st_id, month, year))
 
        chart_data: dict = {}
        for day, category, unit, title, avg, mn, mx in cur.fetchall():
            if category not in chart_data:
                chart_data[category] = {"unit": unit, "title": title, "data": []}
            chart_data[category]["data"].append({
                "day": day,
                "avg": float(avg) if avg is not None else None,
                "min": float(mn) if mn is not None else None,
                "max": float(mx) if mx is not None else None,
            })
 
        # sensor summary
        cur.execute("""
            SELECT DISTINCT ON (se.se_id)
                se.se_id,
                se.title,
                se.category,
                se.unit,
                ROUND(AVG(re.avg_value) OVER (PARTITION BY se.se_id)::numeric, 2) AS avg_value,
                ROUND(FIRST_VALUE(re.avg_value) OVER (
                    PARTITION BY se.se_id ORDER BY re.date DESC
                )::numeric, 2) AS latest_value
            FROM readings re
            JOIN sensors se ON re.se_id = se.se_id
            WHERE se.st_id = %s
              AND EXTRACT(MONTH FROM re.date) = %s
              AND EXTRACT(YEAR FROM re.date) = %s
            ORDER BY se.se_id, re.date DESC;
        """, (st_id, month, year))
 
        sensors = [
            {
                "se_id": r[0], "title": r[1], "category": r[2], "unit": r[3],
                "avg": float(r[4]) if r[4] is not None else None,
                "latest": float(r[5]) if r[5] is not None else None,
            }
            for r in cur.fetchall()
        ]
 
        cur.close()
 
    finally:
        release_conn(conn)
 
    return {
        "station": station,
        "period": {"month": month, "year": year},
        "chart_data": chart_data,
        "sensors": sensors,
    }

# Country/Regional level data with download option
@app.get("/country_region_data")
def country_region_data(
    download: Optional[bool] = Query(False),
    type: Optional[List[OutputType]] = Query(None),
    from_date: Optional[date] = Query(date(2014, 6, 3)),
    to_date: Optional[date] = Query(None),
    category: Optional[str] = Query("all"),
    country: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
):
    output_types: List[OutputType] = type if type else ["csv"]

    categories = _split_csv_filter(category) if category and category.lower() != "all" else []
    countries = _split_csv_filter(country)
    regions = _split_csv_filter(region)

    if not download:
        cache_key = json.dumps({
            "from_date": from_date.isoformat() if from_date else None,
            "to_date": to_date.isoformat() if to_date else None,
            "categories": categories,
            "countries": countries,
            "regions": regions,
        }, sort_keys=True)
        cached = _country_region_cache.get(cache_key)
        if cached:
            return cached
 
    conn = get_db_connection()
    try:
        cur = conn.cursor()
 
        sf_filters = []
        sf_params = []
        if from_date:
            sf_filters.append("date >= %s")
            sf_params.append(from_date)
        if to_date:
            sf_filters.append("date <= %s")
            sf_params.append(to_date)
        sf_where = ("WHERE " + " AND ".join(sf_filters)) if sf_filters else ""

        query = f'''
            SELECT
                st.country,
                st.region,
                COUNT(DISTINCT st.st_id) AS total_stations,
                COUNT(*) AS total_sensors
            FROM (
                SELECT DISTINCT se_id
                FROM sensor_files
                {sf_where}
            ) sf_agg
            INNER JOIN sensors se ON se.se_id = sf_agg.se_id
            INNER JOIN stations st ON st.st_id = se.st_id
            WHERE st.country IS NOT NULL
        '''
        params = sf_params

        if categories:
            query += f" AND se.category IN ({','.join(['%s']*len(categories))})"
            params.extend(categories)
        if countries:
            query += f" AND st.country IN ({','.join(['%s']*len(countries))})"
            params.extend(countries)
        if regions:
            query += f" AND st.region IN ({','.join(['%s']*len(regions))})"
            params.extend(regions)
 
        query += " GROUP BY st.country, st.region ORDER BY st.country, st.region"
        cur.execute(query, tuple(params))
        rows = cur.fetchall()

        result = [
            {
                "country": r[0], 
                "region": r[1],
                "total_stations": r[2], 
                "total_sensors": r[3],
            }
            for r in rows
        ]

        if not download:
            cur.close()
            _country_region_cache.set(cache_key, result)
            return result

        # Download
        cur.close()
        dl_cur = conn.cursor(name="crd_download_cursor")
        dl_cur.itersize = 2000

        url_query = '''
            SELECT
                st.st_id, st.name, st.exposure, st.model,
                ST_Y(st.location::geometry) AS latitude,
                ST_X(st.location::geometry) AS longitude,
                st.country, st.region,
                se.se_id, se.title, se.type, se.category, se.unit,
                sf.date, sf.csv_url
            FROM stations st
            JOIN sensors se ON st.st_id = se.st_id
            JOIN sensor_files sf ON se.se_id = sf.se_id
            WHERE st.country IS NOT NULL
        '''
        url_params = []

        if from_date:
            url_query += " AND sf.date >= %s"; url_params.append(from_date)
        if to_date:
            url_query += " AND sf.date <= %s"; url_params.append(to_date)
        if categories:
            url_query += f" AND se.category IN ({','.join(['%s']*len(categories))})"
            url_params.extend(categories)
        if countries:
            url_query += f" AND st.country IN ({','.join(['%s']*len(countries))})"
            url_params.extend(countries)
        if regions:
            url_query += f" AND st.region IN ({','.join(['%s']*len(regions))})"
            url_params.extend(regions)
 
        url_query += " ORDER BY sf.date"
        dl_cur.execute(url_query, tuple(url_params))
 
        # Collect via batches
        url_rows = []
        while True:
            batch = dl_cur.fetchmany(2000)
            if not batch:
                break
            url_rows.extend(batch)
 
        dl_cur.close()
 
    finally:
        release_conn(conn)
 
    country_str = country.replace(",", "-").replace(" ", "_") if country else "all"
    region_str = region.replace(",", "-").replace(" ", "_")  if region  else "all"
    from_str = from_date.strftime("%Y%m%d") if from_date else "start"
    to_str = to_date.strftime("%Y%m%d") if to_date else date.today().strftime("%Y%m%d")
    num_days = ((to_date - from_date).days if (to_date and from_date) else (date.today() - from_date).days if from_date else 0)
    base_filename = f"{country_str}_{region_str}_{from_str}_{to_str}_{num_days}d"
 
    return build_output_file(url_rows, output_types=output_types, base_filename=base_filename)
 

# Bounding box query with download option
@app.get("/bbox_data")
def bbox_data(
    aoi: str = Query(..., description="GeoJSON Feature or Polygon as URL-encoded string"),
    from_date: Optional[date] = Query(date(2014, 6, 3)),
    to_date: Optional[date] = Query(None),
    category: Optional[str] = Query("all"),
    download: Optional[bool] = Query(False),
    type: Optional[List[OutputType]] = Query(None),
):
    output_types: List[OutputType] = type if type else ["csv"]
 
    try:
        aoi_json = json.loads(unquote(aoi))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid AOI: could not parse GeoJSON.")
 
    if aoi_json.get("type") == "Feature":
        geometry = aoi_json.get("geometry")
    elif aoi_json.get("type") == "Polygon":
        geometry = aoi_json
    elif aoi_json.get("type") == "FeatureCollection":
        features = aoi_json.get("features", [])
        geometry = next(
            (f["geometry"] for f in features if f.get("geometry", {}).get("type") == "Polygon"),
            None,
        )
    else:
        geometry = None
 
    if not geometry or geometry.get("type") != "Polygon":
        raise HTTPException(status_code=400, detail="AOI must contain a Polygon geometry.")
 
    geojson_str = json.dumps(geometry)
 
    conn = get_db_connection()
    try:
        cur = conn.cursor()
 
        # Date filters for sensor_files subquery
        sf_filters, sf_params = [], []
        if from_date:
            sf_filters.append("date >= %s"); sf_params.append(from_date)
        if to_date:
            sf_filters.append("date <= %s"); sf_params.append(to_date)
        sf_where = ("WHERE " + " AND ".join(sf_filters)) if sf_filters else ""
 
        # Date filters for readings subquery
        re_filters, re_params = [], []
        if from_date:
            re_filters.append("date >= %s"); re_params.append(from_date)
        if to_date:
            re_filters.append("date <= %s"); re_params.append(to_date)
        re_where = ("WHERE " + " AND ".join(re_filters)) if re_filters else ""
 
        # Bounding-box pre-filter (hits GIST index) before the exact ST_Within check
        query = f'''
            SELECT
                st.country,
                st.region,
                COUNT(DISTINCT st.st_id) AS total_stations,
                COUNT(DISTINCT se.se_id) AS total_sensors,
                COALESCE(SUM(re_agg.total), 0) AS total_readings,
                COALESCE(SUM(sf_agg.size_mb), 0) AS estimated_size
            FROM stations st
            INNER JOIN sensors se ON st.st_id = se.st_id
            INNER JOIN (
                SELECT se_id, SUM(size_mb) AS size_mb
                FROM sensor_files
                {sf_where}
                GROUP BY se_id
            ) sf_agg ON se.se_id = sf_agg.se_id
            INNER JOIN (
                SELECT se_id, SUM(count) AS total
                FROM readings
                {re_where}
                GROUP BY se_id
            ) re_agg ON se.se_id = re_agg.se_id
            WHERE st.location IS NOT NULL
              AND st.location && ST_GeomFromGeoJSON(%s)
              AND ST_Within(st.location::geometry, ST_GeomFromGeoJSON(%s))
        '''
 
        params = sf_params + re_params + [geojson_str, geojson_str]
 
        if category and category.lower() != "all":
            cats = [c.strip() for c in category.split(",")]
            query += f" AND se.category IN ({','.join(['%s']*len(cats))})"
            params.extend(cats)
 
        query += " GROUP BY st.country, st.region ORDER BY st.country, st.region"
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
 
        result = []
        total_stations_sum = total_sensors_sum = total_readings_sum = 0
        total_size_sum = 0.0
 
        for row_country, row_region, total_stations, total_sensors, total_readings, estimated_size in rows:
            total_stations_sum += total_stations
            total_sensors_sum += total_sensors
            total_readings_sum += int(total_readings)
            total_size_sum += float(estimated_size)
            result.append({
                "country": row_country, "region": row_region,
                "total_stations": total_stations, "total_sensors": total_sensors,
                "total_readings": int(total_readings), "estimated_size": float(estimated_size),
            })
 
        if not download:
            cur.close()
            return {
                "summary": {
                    "total_stations": total_stations_sum,
                    "total_sensors": total_sensors_sum,
                    "total_readings": total_readings_sum,
                    "estimated_size": round(total_size_sum, 6),
                },
                "data": result,
            }
 
        # Download iterate server side for large downloads
        cur.close()
        dl_cur = conn.cursor(name="bbox_download_cursor")
        dl_cur.itersize = 2000
 
        url_query = '''
            SELECT
                st.st_id, st.name, st.exposure, st.model,
                ST_Y(st.location::geometry) AS latitude,
                ST_X(st.location::geometry) AS longitude,
                st.country, st.region,
                se.se_id, se.title, se.type, se.category, se.unit,
                sf.date, sf.csv_url
            FROM stations st
            JOIN sensors se ON st.st_id = se.st_id
            JOIN sensor_files sf ON se.se_id = sf.se_id
            WHERE st.location IS NOT NULL
              AND st.location && ST_GeomFromGeoJSON(%s)
              AND ST_Within(st.location::geometry, ST_GeomFromGeoJSON(%s))
        '''
        url_params = [geojson_str, geojson_str]
 
        if from_date:
            url_query += " AND sf.date >= %s"; url_params.append(from_date)
        if to_date:
            url_query += " AND sf.date <= %s"; url_params.append(to_date)
        if category and category.lower() != "all":
            cats = [c.strip() for c in category.split(",")]
            url_query += f" AND se.category IN ({','.join(['%s']*len(cats))})"
            url_params.extend(cats)
 
        url_query += " ORDER BY sf.date"
        dl_cur.execute(url_query, tuple(url_params))
 
        url_rows = []
        while True:
            batch = dl_cur.fetchmany(2000)
            if not batch:
                break
            url_rows.extend(batch)
 
        dl_cur.close()
 
    finally:
        release_conn(conn)
 
    now = datetime.now()
    num_days = ((to_date - from_date).days if (to_date and from_date) else (date.today() - from_date).days if from_date else 0)
    base_filename = f"aoi_{now.strftime('%Y%m%d')}_{now.strftime('%H%M%S')}_{num_days}d"
 
    return build_output_file(url_rows, output_types=output_types, base_filename=base_filename)
