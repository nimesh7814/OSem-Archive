from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException
from typing import Optional
from datetime import date
from urllib.parse import unquote
from utils import build_download_zip
from datetime import datetime
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

@app.get("/")
def status():
    return {"message": "OK!"}

@app.get("/summary")
def get_summary():
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM stations")
    total_stations = cur.fetchone()[0] or 0
    
    cur.execute("SELECT COUNT(*) FROM sensors")
    total_sensors = cur.fetchone()[0] or 0
    
    cur.execute("SELECT SUM(count) FROM readings")
    total_readings = cur.fetchone()[0] or 0
    
    cur.execute("SELECT COUNT(DISTINCT (country)) FROM stations WHERE country IS NOT NULL")
    total_countries = cur.fetchone()[0] or 0
    
    cur.close()
    conn.close()
    
    return {
        "total_stations": total_stations,
        "total_sensors": total_sensors,
        "total_readings": total_readings,
        "total_countries": total_countries
    }

@app.get("/countries")
def get_countries():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT country FROM stations WHERE country IS NOT NULL")
    countries = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {"countries": countries}

@app.get("/regions")
def get_regions(country: str):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT region FROM stations WHERE country = %s AND region IS NOT NULL",(country,))
    regions = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {"regions": regions}

@app.get("/sensor_categories")
def get_sensor_categories():
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT DISTINCT category FROM sensors WHERE category IS NOT NULL")
    categories = [row[0] for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return {"categories": categories}

@app.get("/get_stations")
def get_stations(station_id: Optional[str] = Query(None, description="Optional station ID")):
    conn = get_db_connection()
    cur = conn.cursor()

    if station_id:
        # When clicked on a station
        cur.execute('''
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
    else:
        # General endpoint for map markers
        cur.execute('''
            SELECT
                st_id,
                name,
                ST_Y(location::geometry) AS latitude,
                ST_X(location::geometry) AS longitude
            FROM stations
            WHERE location IS NOT NULL
        ''')

    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return []

    # Response of the API
    if station_id:
        # Detailed station info
        result = {
            "st_id": rows[0][0],
            "name": rows[0][1],
            "country": rows[0][2],
            "region": rows[0][3],
            "latitude": rows[0][4],
            "longitude": rows[0][5],
            "total_sensors": rows[0][6],
            "total_readings": rows[0][7],
            "categories": rows[0][8]
        }
    else:
        # General endpoint for map markers
        result = [{
            "st_id": r[0],
            "name": r[1],
            "latitude": r[2],
            "longitude": r[3]} for r in rows]

    return result

@app.get("/station_readings")
def station_readings(
    st_id: str = Query(..., description="Station ID"),
    month: int = Query(..., description="Month as integer e.g. 5"),
    year:  int = Query(..., description="Year as integer e.g. 2026"),
):
    conn = get_db_connection()
    cur = conn.cursor()

    # Station metadata
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
        cur.close()
        conn.close()
        return {"error": f"Station '{st_id}' not found."}

    station = {
        "st_id": row[0],
        "name": row[1],
        "latitude": float(row[2]) if row[2] is not None else None,
        "longitude": float(row[3]) if row[3] is not None else None,
        "total_sensors": row[4],
        "total_readings": int(row[5]) if row[5] is not None else 0,
        "categories": row[6],
        "country": row[7],
        "region": row[8],
    }

    # Daily avg per sensor category
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
          AND EXTRACT(YEAR FROM re.date) = %s
          AND se.category IS NOT NULL
        GROUP BY day, se.category, se.unit, se.title
        ORDER BY day, se.category;
    """, (st_id, month, year))

    chart_data: dict[str, dict] = {}
    for day, category, unit, title, avg, mn, mx in cur.fetchall():
        if category not in chart_data:
            chart_data[category] = {
                "unit": unit,
                "title": title,
                "data": [],
            }
        chart_data[category]["data"].append({
            "day": day,
            "avg": float(avg) if avg is not None else None,
            "min": float(mn) if mn is not None else None,
            "max": float(mx) if mx is not None else None,
        })

    # Per-sensor summary cards
    cur.execute("""
        SELECT
            se.se_id,
            se.title,
            se.category,
            se.unit,
            ROUND(AVG(re.avg_value)::numeric, 2) AS avg_value,
            (
                SELECT ROUND(re2.avg_value::numeric, 2)
                FROM readings re2
                WHERE re2.se_id = se.se_id
                  AND EXTRACT(MONTH FROM re2.date) = %s
                  AND EXTRACT(YEAR FROM re2.date) = %s
                ORDER BY re2.date DESC
                LIMIT 1
            ) AS latest_value
        FROM readings re
        JOIN sensors se ON re.se_id = se.se_id
        WHERE se.st_id = %s
          AND EXTRACT(MONTH FROM re.date) = %s
          AND EXTRACT(YEAR FROM re.date) = %s
        GROUP BY se.se_id, se.title, se.category, se.unit
        ORDER BY se.category, se.se_id;
    """, (month, year, st_id, month, year))

    sensors = []
    for row in cur.fetchall():
        sensors.append({
            "se_id": row[0],
            "title": row[1],
            "category": row[2],
            "unit": row[3],
            "avg": float(row[4]) if row[4] is not None else None,
            "latest": float(row[5]) if row[5] is not None else None,
        })

    cur.close()
    conn.close()

    # Print the Results
    return {
        "station": station,
        "period": {"month": month, "year": year},
        "chart_data": chart_data,
        "sensors": sensors,
    }

@app.get("/country_region_data")
def country_region_data(
    download: Optional[bool] = Query(False, description="Set true to download ZIP"),
    from_date: Optional[date] = Query(date(2014, 6, 3), description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    category: Optional[str] = Query("all", description="Sensor category (single or comma-separated)"),
    country: Optional[str] = Query(None, description="Country filter (single or comma-separated)"),
    region: Optional[str] = Query(None, description="Region filter (single or comma-separated)")
):
    
    conn = get_db_connection()
    cur = conn.cursor()

    # Remove unnecessary joins for summary query
    sf_filters = []
    sf_params = []
    
    if from_date:
        sf_filters.append("date >= %s")
        sf_params.append(from_date)
    if to_date:
        sf_filters.append("date <= %s")
        sf_params.append(to_date)

    sf_where = ("WHERE " + " AND ".join(sf_filters)) if sf_filters else ""

    # Build the main query
    query = f'''
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_id) AS total_stations,
            COUNT(DISTINCT se.se_id) AS total_sensors,
            COALESCE(SUM(sf_agg.size_mb), 0) AS estimated_size
        FROM stations st
        INNER JOIN sensors se ON st.st_id = se.st_id
        INNER JOIN (
            SELECT se_id, SUM(size_mb) AS size_mb
            FROM sensor_files
            {sf_where}
            GROUP BY se_id
        ) sf_agg ON se.se_id = sf_agg.se_id
        WHERE st.country IS NOT NULL
    '''

    params = sf_params[:]
    
    # Apply category, country, and region filters
    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        query += f" AND se.category IN ({placeholders})"
        params.extend(categories)
        
    if country:
        countries = [c.strip() for c in country.split(",")]
        placeholders = ",".join(["%s"] * len(countries))
        query += f" AND st.country IN ({placeholders})"
        params.extend(countries)
        
    if region:
        regions = [r.strip() for r in region.split(",")]
        placeholders = ",".join(["%s"] * len(regions))
        query += f" AND st.region IN ({placeholders})"
        params.extend(regions)

    query += " GROUP BY st.country, st.region ORDER BY st.country, st.region"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    
    # Build the result dictionary
    result = []
    for row_country, row_region, total_stations, total_sensors, estimated_size in rows:
        result.append({
            "country": row_country,
            "region": row_region,
            "total_stations": total_stations,
            "total_sensors": total_sensors,
            "estimated_size_mb": float(estimated_size)
        })

    if not download:
        cur.close()
        conn.close()
        return result
    
    # URLs query for the download
    url_query = '''
        SELECT
            st.st_id,
            st.name,
            st.exposure,
            st.model,
            ST_Y(st.location::geometry) AS latitude,
            ST_X(st.location::geometry) AS longitude,
            st.country,
            st.region,
            se.se_id,
            se.title,
            se.type,
            se.category,
            se.unit,
            sf.date,
            sf.csv_url
        FROM stations st
        JOIN sensors se ON st.st_id = se.st_id
        JOIN sensor_files sf ON se.se_id = sf.se_id
        WHERE st.country IS NOT NULL
    '''
    
    url_params = []

    # Apply the same filters to the URL query
    if from_date:
        url_query += " AND sf.date >= %s"
        url_params.append(from_date)
    if to_date:
        url_query += " AND sf.date <= %s"
        url_params.append(to_date)
        
    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        url_query += f" AND se.category IN ({placeholders})"
        url_params.extend(categories)
        
    if country:
        countries = [c.strip() for c in country.split(",")]
        placeholders = ",".join(["%s"] * len(countries))
        url_query += f" AND st.country IN ({placeholders})"
        url_params.extend(countries)
    if region:
        regions = [r.strip() for r in region.split(",")]
        placeholders = ",".join(["%s"] * len(regions))
        url_query += f" AND st.region IN ({placeholders})"
        url_params.extend(regions)
        
    url_query += " ORDER BY sf.date"
    cur.execute(url_query, tuple(url_params))
    url_rows = cur.fetchall()

    cur.close()
    conn.close()

    # Build zip filename
    country_str = country.replace(",", "-").replace(" ", "_") if country else "all"
    region_str = region.replace(",", "-").replace(" ", "_")  if region  else "all"
    from_str = from_date.strftime("%Y%m%d") if from_date else "start"
    to_str = to_date.strftime("%Y%m%d") if to_date else date.today().strftime("%Y%m%d")
    
    num_days = (to_date - from_date).days if (to_date and from_date) else (date.today() - from_date).days
   
    # Filename format
    zip_filename = f"{country_str}_{region_str}_{from_str}_{to_str}_{num_days}d.zip"

    return build_download_zip(url_rows, zip_filename=zip_filename)
    
@app.get("/bbox_data")
def bbox_data(
    aoi: str = Query(..., description="GeoJSON Feature or Polygon as URL-encoded string"),
    from_date: Optional[date] = Query(date(2014, 6, 3), description="Start date (YYYY-MM-DD)"),
    to_date: Optional[date] = Query(None, description="End date (YYYY-MM-DD)"),
    category: Optional[str] = Query("all", description="Sensor category (single or comma-separated)"),
    download: Optional[bool] = Query(False, description="Set true to download ZIP")
):
    
    # Parse AOI GeoJSON
    try:
        aoi_json = json.loads(unquote(aoi))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid AOI: could not parse GeoJSON.")
    
    # Accept Features
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
    
    # Pre-aggregate sensor_files subquery for optimized filtering
    sf_filters = []
    sf_params = []
    if from_date:
        sf_filters.append("date >= %s")
        sf_params.append(from_date)
    if to_date:
        sf_filters.append("date <= %s")
        sf_params.append(to_date)

    sf_where = ("WHERE " + " AND ".join(sf_filters)) if sf_filters else ""
    
    # Main Query
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
            {sf_where}
            GROUP BY se_id
        ) re_agg ON se.se_id = re_agg.se_id
        WHERE st.location IS NOT NULL
        AND ST_Within(
                st.location::geometry,
                ST_GeomFromGeoJSON(%s)
            )
    '''

    params = sf_params + sf_params + [geojson_str]
    
    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        query += f" AND se.category IN ({placeholders})"
        params.extend(categories)

    query += " GROUP BY st.country, st.region ORDER BY st.country, st.region"
    cur.execute(query, tuple(params))
    rows = cur.fetchall()

    # Build the result dictionary
    result = []
    total_stations_sum = 0
    total_sensors_sum  = 0
    total_readings_sum = 0
    total_size_sum = 0.0

    for row_country, row_region, total_stations, total_sensors, total_readings, estimated_size in rows:
        total_stations_sum += total_stations
        total_sensors_sum += total_sensors
        total_readings_sum += int(total_readings)
        total_size_sum += float(estimated_size)
        result.append({
            "country": row_country,
            "region": row_region,
            "total_stations": total_stations,
            "total_sensors": total_sensors,
            "total_readings": int(total_readings),
            "estimated_size": float(estimated_size)
        })

    if not download:
        cur.close()
        conn.close()
        return {
            "summary": {
                "total_stations": total_stations_sum,
                "total_sensors": total_sensors_sum,
                "total_readings": total_readings_sum,
                "estimated_size": round(total_size_sum, 6)
            },
            "data": result
        }
    
    # URLs query for the download
    url_query = '''
        SELECT
            st.st_id,
            st.name,
            st.exposure,
            st.model,
            ST_Y(st.location::geometry) AS latitude,
            ST_X(st.location::geometry) AS longitude,
            st.country,
            st.region,
            se.se_id,
            se.title,
            se.type,
            se.category,
            se.unit,
            sf.date,
            sf.csv_url
        FROM stations st
        JOIN sensors se ON st.st_id = se.st_id
        JOIN sensor_files sf ON se.se_id = sf.se_id
        WHERE st.location IS NOT NULL
          AND ST_Within(
                st.location::geometry,
                ST_GeomFromGeoJSON(%s)
              )
    '''

    url_params = [geojson_str]
    
    # Apply the same filters to the URL query
    if from_date:
        url_query += " AND sf.date >= %s"
        url_params.append(from_date)
    if to_date:
        url_query += " AND sf.date <= %s"
        url_params.append(to_date)
        
    if category and category.lower() != "all":
        categories = [c.strip() for c in category.split(",")]
        placeholders = ",".join(["%s"] * len(categories))
        url_query += f" AND se.category IN ({placeholders})"
        url_params.extend(categories)

    url_query += " ORDER BY sf.date"
    cur.execute(url_query, tuple(url_params))
    url_rows = cur.fetchall()

    cur.close()
    conn.close()
    
    # Build bbox zip filename
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M%S")
    num_days = (to_date - from_date).days if (to_date and from_date) else (date.today() - from_date).days

    zip_filename = f"aoi_{date_str}_{time_str}_{num_days}d.zip"

    return build_download_zip(url_rows, zip_filename=zip_filename)