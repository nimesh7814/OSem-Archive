from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException
from typing import Literal, Optional, List
from datetime import date, datetime
from urllib.parse import unquote
from utils import build_output_file, OutputType, AggregateType
import json
import psycopg2
import os
import math
 
load_dotenv()

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5436)),
        database=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )
    
app = FastAPI(title="OpenSenseMap Index API")

AggregateType = Literal["raw", "day", "month", "year"]

@app.get("/")
def status():
    return {"message": "OK!"}

@app.get("/summary")
def get_summary():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COUNT(DISTINCT st.st_id) AS total_stations,
            COUNT(DISTINCT se.se_id) AS total_sensors,
            COALESCE(SUM(rc.reading_count),0) AS total_readings,
            COUNT(DISTINCT st.country) AS total_countries
        FROM stations st
        LEFT JOIN sensors se ON se.st_id = st.st_id
        LEFT JOIN readings_daily rc ON rc.se_id = se.se_id
        WHERE st.country IS NOT NULL;
    """)

    row = cur.fetchone()
    cur.close()
    conn.close()

    return {
        "total_stations": row[0],
        "total_sensors": row[1],
        "total_readings": row[2],
        "total_countries": row[3]
    }

@app.get("/countries")
def get_countries():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT country FROM stations WHERE country IS NOT NULL ORDER BY country")
    countries = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {"countries": countries}

@app.get("/regions")
def get_regions(country: str):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute(
        "SELECT DISTINCT region FROM stations WHERE country = %s AND region IS NOT NULL ORDER BY region",
        (country,)
    )
    regions = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {"regions": regions}

@app.get("/sensor_categories")
def get_sensor_categories():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT type FROM sensors WHERE type IS NOT NULL ORDER BY type")
    categories = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {"categories": categories}

from typing import Optional
from fastapi import Query

@app.get("/get_stations")
def get_stations(station_id: Optional[str] = Query(None, description="Optional station ID")):
    conn = get_db_connection()
    cur = conn.cursor()

    if station_id:
        cur.execute('''
            SELECT
                st.st_id,
                st.country,
                st.region,
                ST_Y(st.geometry) AS latitude,
                ST_X(st.geometry) AS longitude,
                COUNT(DISTINCT se.se_id) AS total_sensors,
                COALESCE(SUM(rd.reading_count), 0) AS total_readings,
                STRING_AGG(DISTINCT se.type, ', ') AS sensor_types
            FROM stations st
            JOIN sensors se ON st.st_id = se.st_id
            LEFT JOIN readings_daily rd ON se.se_id = rd.se_id
            WHERE st.st_id = %s
            GROUP BY st.st_id, st.country, st.region, st.geometry
        ''', (station_id,))
    else:
        cur.execute('''
            SELECT
                st_id,
                ST_Y(geometry) AS latitude,
                ST_X(geometry) AS longitude
            FROM stations
            WHERE geometry IS NOT NULL
        ''')

    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return []

    if station_id:
        result = {
            "st_id": rows[0][0],
            "country": rows[0][1],
            "region": rows[0][2],
            "latitude": rows[0][3],
            "longitude": rows[0][4],
            "total_sensors": rows[0][5],
            "total_readings": rows[0][6],
            "sensor_types": rows[0][7],
        }
    else:
        result = [
            {"st_id": r[0], "latitude": r[1], "longitude": r[2]}
            for r in rows
        ]

    return result

@app.get("/station_readings")
def station_readings(
    st_id: str = Query(..., description="Station ID"),
    month: int = Query(..., description="Month as integer e.g. 5"),
    year: int = Query(..., description="Year as integer e.g. 2026"),
):
    conn = get_db_connection()
    cur = conn.cursor()

    # Available periods
    cur.execute("""
        SELECT DISTINCT
            EXTRACT(YEAR FROM rd.bucket)::int AS year,
            EXTRACT(MONTH FROM rd.bucket)::int AS month
        FROM readings_daily rd
        JOIN sensors se ON se.se_id = rd.se_id
        WHERE se.st_id = %s
        ORDER BY year, month
    """, (st_id,))

    rows = cur.fetchall()
    if not rows:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail=f"No data found for station '{st_id}'.")

    available_periods = [{"year": r[0], "month": r[1]} for r in rows]

    cur.execute("""
        SELECT
            st.st_id,
            ST_Y(st.geometry) AS latitude,
            ST_X(st.geometry) AS longitude,
            COUNT(DISTINCT se.se_id) AS total_sensors,
            COALESCE(SUM(rd.reading_count), 0) AS total_readings,
            STRING_AGG(DISTINCT se.type, ', ') AS categories,
            st.country,
            st.region
        FROM stations st
        JOIN sensors se ON se.st_id = st.st_id
        LEFT JOIN readings_daily rd
            ON  rd.se_id = se.se_id
            AND EXTRACT(MONTH FROM rd.bucket) = %s
            AND EXTRACT(YEAR FROM rd.bucket) = %s
        WHERE st.st_id = %s
        GROUP BY st.st_id, st.geometry, st.country, st.region
    """, (month, year, st_id))

    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail=f"Station '{st_id}' not found.")

    station = {
        "st_id": row[0],
        "latitude": float(row[1]) if row[1] is not None else None,
        "longitude": float(row[2]) if row[2] is not None else None,
        "total_sensors": row[3],
        "total_readings": int(row[4]),
        "categories": row[5],
        "country": row[6],
        "region": row[7],
    }

    cur.execute("""
        SELECT DISTINCT ON (se.se_id, EXTRACT(DAY FROM rd.bucket)::int)
            se.se_id,
            se.title,
            se.type,
            se.unit,
            EXTRACT(DAY FROM rd.bucket)::int AS day,
            ROUND(AVG(rd.avg_value) OVER (PARTITION BY se.se_id)::numeric, 2) AS sensor_avg,
            ROUND(AVG(rd.avg_value) OVER (
                PARTITION BY se.type, EXTRACT(DAY FROM rd.bucket))::numeric, 2) AS day_avg,
            ROUND(LAST_VALUE(rd.avg_value) OVER (
                PARTITION BY se.se_id ORDER BY rd.bucket
                ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
            )::numeric, 2) AS sensor_latest
        FROM readings_daily rd
        JOIN sensors se ON se.se_id = rd.se_id
        WHERE se.st_id = %s
          AND EXTRACT(MONTH FROM rd.bucket) = %s
          AND EXTRACT(YEAR  FROM rd.bucket) = %s
          AND se.type IS NOT NULL
        ORDER BY se.se_id, day, rd.bucket
    """, (st_id, month, year))

    chart_data: dict[str, dict] = {}
    seen_sensors: dict[str, dict] = {}

    for se_id, title, sensor_type, unit, day, sensor_avg, day_avg, sensor_latest in cur.fetchall():
        if sensor_type not in chart_data:
            chart_data[sensor_type] = {"unit": unit, "title": title, "data": []}
        chart_data[sensor_type]["data"].append({
            "day": day,
            "avg": float(day_avg) if day_avg is not None else None,
        })
        seen_sensors[se_id] = {
            "se_id": se_id,
            "title": title,
            "category": sensor_type,
            "unit": unit,
            "avg": float(sensor_avg) if sensor_avg is not None else None,
            "latest": float(sensor_latest) if sensor_latest is not None else None,
        }

    sensors = list(seen_sensors.values())

    cur.close()
    conn.close()

    return {
        "station": station,
        "period": {"month": month, "year": year},
        "available_periods": available_periods,
        "chart_data": chart_data,
        "sensors": sensors,
    }
    
@app.get("/country_region_data")
def country_region_data(
    download: Optional[bool] = Query(False, description="Set true to download output file(s)"),
    type: Optional[List[str]] = Query(None, description="Output format(s): csv, geojson, json, all"),
    from_date: Optional[date] = Query(date(2014, 6, 3), description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    category: Optional[str] = Query("all", description="Sensor type filter (single or comma-separated)"),
    country: Optional[str] = Query(None, description="Country filter (single or comma-separated)"),
    region: Optional[str] = Query(None, description="Region filter (single or comma-separated)"),
    aggregate: Optional[AggregateType] = Query(None, description="Aggregation level: raw, day, month, year (default: raw)"),
):
    _ALL_TYPES: List[OutputType] = ["csv", "geojson", "json"]
    if type and "all" in type:
        output_types: List[OutputType] = _ALL_TYPES
    else:
        output_types: List[OutputType] = type if type else ["csv"]
    aggregate = aggregate or "raw"

    conn = get_db_connection()
    cur = conn.cursor()

    def build_filters(category, country, region):
        filters = ["st.country IS NOT NULL"]
        params = []
        if category and category.lower() != "all":
            cats = [c.strip() for c in category.split(",")]
            filters.append(f"se.type IN ({','.join(['%s'] * len(cats))})")
            params.extend(cats)
        if country:
            countries = [c.strip() for c in country.split(",")]
            filters.append(f"st.country IN ({','.join(['%s'] * len(countries))})")
            params.extend(countries)
        if region:
            regions = [r.strip() for r in region.split(",")]
            filters.append(f"st.region IN ({','.join(['%s'] * len(regions))})")
            params.extend(regions)
        return filters, params

    # Date join fragment for summary (always uses readings_daily)
    date_params = []
    date_join_filters = []
    if from_date:
        date_join_filters.append("rd.bucket >= %s")
        date_params.append(from_date)
    if to_date:
        date_join_filters.append("rd.bucket <= %s")
        date_params.append(to_date)
    date_join_sql = (" AND " + " AND ".join(date_join_filters)) if date_join_filters else ""

    # Summary query — always uses readings_daily
    filters, params = build_filters(category, country, region)
    summary_query = f"""
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_id) AS total_stations,
            COUNT(DISTINCT se.se_id) AS total_sensors,
            COALESCE(SUM(rd.reading_count), 0) AS total_readings
        FROM stations st
        JOIN sensors se ON se.st_id = st.st_id
        LEFT JOIN readings_daily rd
            ON  rd.se_id = se.se_id
            {date_join_sql}
        WHERE {" AND ".join(filters)}
        GROUP BY st.country, st.region
        ORDER BY st.country, st.region
    """

    cur.execute(summary_query, tuple(date_params + params))
    rows = cur.fetchall()

    result = []
    for row_country, row_region, total_stations, total_sensors, total_readings in rows:
        result.append({
            "country": row_country,
            "region": row_region,
            "total_stations": total_stations,
            "total_sensors":  total_sensors,
            "total_readings": int(total_readings),
        })

    if not download:
        cur.close()
        conn.close()
        return result

    # Download query — source depends on aggregate
    filters, params = build_filters(category, country, region)

    if aggregate == "raw":
        raw_date_params = []
        raw_date_filters = []
        if from_date:
            raw_date_filters.append("r.time >= %s")
            raw_date_params.append(from_date)
        if to_date:
            raw_date_filters.append("r.time <= %s")
            raw_date_params.append(to_date)
        raw_date_join_sql = (" AND " + " AND ".join(raw_date_filters)) if raw_date_filters else ""

        download_query = f"""
            SELECT
                st.st_id,
                st.exposure,
                st.model,
                ST_Y(st.geometry) AS latitude,
                ST_X(st.geometry) AS longitude,
                st.country,
                st.region,
                se.se_id,
                se.title,
                se.type,
                se.unit,
                r.time AS time,
                r.value AS value
            FROM stations st
            JOIN sensors se ON se.st_id = st.st_id
            JOIN readings r
                ON  r.se_id = se.se_id
                {raw_date_join_sql}
            WHERE {" AND ".join(filters)}
            ORDER BY r.time
        """
        download_params = tuple(raw_date_params + params)

    else:
        agg_table = {
            "day":   "readings_daily",
            "month": "readings_monthly",
            "year":  "readings_yearly",
        }[aggregate]

        download_query = f"""
            SELECT
                st.st_id,
                st.exposure,
                st.model,
                ST_Y(st.geometry) AS latitude,
                ST_X(st.geometry) AS longitude,
                st.country,
                st.region,
                se.se_id,
                se.title,
                se.type,
                se.unit,
                rd.bucket AS time,
                rd.avg_value AS value
            FROM stations st
            JOIN sensors se ON se.st_id = st.st_id
            JOIN {agg_table} rd
                ON  rd.se_id = se.se_id
                {date_join_sql}
            WHERE {" AND ".join(filters)}
            ORDER BY rd.bucket
        """
        download_params = tuple(date_params + params)

    cur.execute(download_query, download_params)
    url_rows = cur.fetchall()

    cur.close()
    conn.close()

    country_str = country.replace(",", "-").replace(" ", "_") if country else "all"
    region_str = region.replace(",", "-").replace(" ", "_")  if region  else "all"
    from_str = from_date.strftime("%Y%m%d") if from_date else "start"
    to_str = to_date.strftime("%Y%m%d") if to_date else date.today().strftime("%Y%m%d")
    num_days = (
        (to_date - from_date).days           if (to_date and from_date)
        else (date.today() - from_date).days if from_date
        else 0
    )
    base_filename = f"{country_str}_{region_str}_{from_str}_{to_str}_{num_days}d_{aggregate}"

    return build_output_file(url_rows, output_types=output_types, base_filename=base_filename)

@app.get("/bbox_data")
def bbox_data(
    aoi: str = Query(..., description="GeoJSON Feature or Polygon as URL-encoded string"),
    from_date: Optional[date] = Query(date(2014, 6, 3), description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    category: Optional[str] = Query("all", description="Sensor type filter (single or comma-separated)"),
    download: Optional[bool] = Query(False, description="Set true to download output file(s)"),
    type: Optional[List[str]] = Query(None, description="Output format(s): csv, geojson, json, all (default: csv)"),
    aggregate: Optional[AggregateType] = Query(None, description="Aggregation level: raw, day, month, year (default: raw)"),
):
    _ALL_TYPES: List[OutputType] = ["csv", "geojson", "json"]
    if type and "all" in type:
        output_types: List[OutputType] = _ALL_TYPES
    else:
        output_types: List[OutputType] = type if type else ["csv"]
    aggregate = aggregate or "raw"

    # Parse AOI GeoJSON
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
            None
        )
    else:
        geometry = None

    if not geometry or geometry.get("type") != "Polygon":
        raise HTTPException(status_code=400, detail="AOI must contain a Polygon geometry.")

    geojson_str = json.dumps(geometry)

    conn = get_db_connection()
    cur = conn.cursor()

    # Time filters on readings_daily
    rd_filters = []
    rd_params = []
    if from_date:
        rd_filters.append("rd.bucket >= %s")
        rd_params.append(from_date)
    if to_date:
        rd_filters.append("rd.bucket <= %s")
        rd_params.append(to_date)

    rd_where_inner = ("WHERE " + " AND ".join(f.replace("rd.", "") for f in rd_filters)) if rd_filters else ""

    # Summary query
    query = f"""
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_id) AS total_stations,
            COUNT(DISTINCT se.se_id) AS total_sensors,
            COALESCE(SUM(rd_agg.reading_count), 0) AS total_readings
        FROM stations st
        INNER JOIN sensors se ON st.st_id = se.st_id
        INNER JOIN (
            SELECT se_id, COUNT(*) AS reading_count
            FROM readings_daily
            {rd_where_inner}
            GROUP BY se_id
        ) rd_agg ON se.se_id = rd_agg.se_id
        WHERE st.geometry IS NOT NULL
          AND ST_Within(
                st.geometry,
                ST_GeomFromGeoJSON(%s)
              )
    """

    params = rd_params + [geojson_str]

    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        query += f" AND se.type IN ({placeholders})"
        params.extend(categories)

    query += " GROUP BY st.country, st.region ORDER BY st.country, st.region"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()

    result = []
    total_stations_sum = 0
    total_sensors_sum  = 0
    total_readings_sum = 0

    for row_country, row_region, total_stations, total_sensors, total_readings in rows:
        total_stations_sum += total_stations
        total_sensors_sum  += total_sensors
        total_readings_sum += int(total_readings)
        result.append({
            "country": row_country,
            "region": row_region,
            "total_stations": total_stations,
            "total_sensors": total_sensors,
            "total_readings": int(total_readings),
        })

    if not download:
        cur.close()
        conn.close()
        return {
            "summary": {
                "total_stations": total_stations_sum,
                "total_sensors": total_sensors_sum,
                "total_readings": total_readings_sum,
            },
            "data": result
        }

    # Download rows
    if aggregate == "raw":
        raw_date_filters = []
        raw_date_params  = []
        if from_date:
            raw_date_filters.append("r.time >= %s")
            raw_date_params.append(from_date)
        if to_date:
            raw_date_filters.append("r.time <= %s")
            raw_date_params.append(to_date)
        raw_date_join_sql = (" AND " + " AND ".join(raw_date_filters)) if raw_date_filters else ""

        url_query = f"""
            SELECT
                st.st_id,
                st.exposure,
                st.model,
                ST_Y(st.geometry) AS latitude,
                ST_X(st.geometry) AS longitude,
                st.country,
                st.region,
                se.se_id,
                se.title,
                se.type,
                se.unit,
                r.time AS time,
                r.value AS value
            FROM stations st
            JOIN sensors se ON st.st_id = se.st_id
            JOIN readings r
                ON  r.se_id = se.se_id
                {raw_date_join_sql}
            WHERE st.geometry IS NOT NULL
              AND ST_Within(st.geometry, ST_GeomFromGeoJSON(%s))
        """
        url_params = raw_date_params + [geojson_str]

        if category and category.lower() != "all":
            categories = [c.strip() for c in category.split(",")]
            placeholders = ",".join(["%s"] * len(categories))
            url_query += f" AND se.type IN ({placeholders})"
            url_params.extend(categories)

        url_query += " ORDER BY r.time"

    else:
        agg_table = {
            "day": "readings_daily",
            "month": "readings_monthly",
            "year": "readings_yearly",
        }[aggregate]

        agg_date_filters = []
        agg_date_params  = []
        if from_date:
            agg_date_filters.append("rd.bucket >= %s")
            agg_date_params.append(from_date)
        if to_date:
            agg_date_filters.append("rd.bucket <= %s")
            agg_date_params.append(to_date)
        agg_date_join_sql = (" AND " + " AND ".join(agg_date_filters)) if agg_date_filters else ""

        url_query = f"""
            SELECT
                st.st_id,
                st.exposure,
                st.model,
                ST_Y(st.geometry) AS latitude,
                ST_X(st.geometry) AS longitude,
                st.country,
                st.region,
                se.se_id,
                se.title,
                se.type,
                se.unit,
                rd.bucket AS time,
                rd.avg_value AS value
            FROM stations st
            JOIN sensors se ON st.st_id = se.st_id
            JOIN {agg_table} rd
                ON  rd.se_id = se.se_id
                {agg_date_join_sql}
            WHERE st.geometry IS NOT NULL
              AND ST_Within(st.geometry, ST_GeomFromGeoJSON(%s))
        """
        url_params = agg_date_params + [geojson_str]

        if category and category.lower() != "all":
            categories = [c.strip() for c in category.split(",")]
            placeholders = ",".join(["%s"] * len(categories))
            url_query += f" AND se.type IN ({placeholders})"
            url_params.extend(categories)

        url_query += " ORDER BY rd.bucket"

    cur.execute(url_query, tuple(url_params))
    url_rows = cur.fetchall()

    cur.close()
    conn.close()

    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M%S")
    num_days = (
        (to_date - from_date).days if (to_date and from_date)
        else (date.today() - from_date).days if from_date else 0
    )

    base_filename = f"aoi_{date_str}_{time_str}_{num_days}d_{aggregate}"

    return build_output_file(url_rows, output_types=output_types, base_filename=base_filename)