from datetime import datetime
import io
import csv
import zipfile
import requests

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Query
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
import psycopg2
import os

load_dotenv()

app = FastAPI()

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5436"),
        database=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD")
    )


@app.get("/")
def home():
    return {"message": "OK"}

@app.get("/summary")
def get_summary():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM stations")
    total_stations = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM sensors")
    total_sensors = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(count) FROM readings")
    total_readings = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT (country)) FROM stations WHERE country IS NOT NULL")
    total_countries = cursor.fetchone()[0]
    conn.close()
    
    return {
        "stations": total_stations, 
        "sensors": total_sensors, 
        "readings": total_readings, 
        "countries": total_countries
        }
    
@app.get("/countries")
def get_countries():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT country FROM stations WHERE country IS NOT NULL")
    countries = [row[0] for row in cursor.fetchall()]
    conn.close()
    return {"countries": countries}

@app.get("/regions")
def get_regions(country: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT region FROM stations WHERE country = %s AND region IS NOT NULL", (country,))
    regions = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    return {"regions": regions}

@app.get("/sensor_categories")
def get_sensor_categories():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT category FROM sensors WHERE category IS NOT NULL")
    categories = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    return {"categories": categories}

@app.get("/country_region_data")
def country_region_data(
    sensor_type: str = Query("all", description="'all' or comma-separated e.g. 'humidity,temperature'"),
    from_date: str | None = Query(None, description="Optional start date YYYY-MM-DD"),
    to_date: str | None = Query(None, description="Optional end date YYYY-MM-DD"),
    country: str | None = Query(None, description="Optional country filter"),
    region: str | None = Query(None, description="Optional region filter"),
    download: bool = Query(False, description="Set true to download a zip with urls.txt, downloader script and requirements")
):
    conn = get_db_connection()
    cursor = conn.cursor()
 
    effective_from = from_date or '2014-06-03'
    effective_to = to_date or datetime.today().strftime('%Y-%m-%d')
 
    selected_categories = None if sensor_type.lower() == "all" else [s.strip() for s in sensor_type.split(",")]
 
    # Build dynamic filters
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
 
    if not download:
        summary_query = f"""
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_id) AS station_count,
            COUNT(DISTINCT se.se_id) AS sensor_count,
            COALESCE(SUM(sf.size_mb), 0) AS total_size_mb
        FROM sensor_files sf
        JOIN sensors se ON sf.se_id = se.se_id
        JOIN stations st ON sf.st_id = st.st_id
        WHERE sf.date BETWEEN %s AND %s
          AND {where_clause}
        GROUP BY st.country, st.region
        ORDER BY st.country, st.region;
        """
        cursor.execute(summary_query, (effective_from, effective_to, *params))
        rows = cursor.fetchall()
        conn.close()
 
        if not rows:
            return {"message": "No data found for the given filters."}
 
        return {
            "summary": [
                {
                    "country": r[0],
                    "region": r[1],
                    "station_count": r[2],
                    "sensor_count": r[3],
                    "total_size_mb": round(float(r[4]), 2)
                }
                for r in rows
            ]
        }
 
    # Download mode — zip up urls.txt + downloader files
    download_query = f"""
    SELECT
        st.st_id,
        st.name AS station_name,
        st.exposure,
        st.model,
        ST_Y(st.location::geometry) AS latitude,
        ST_X(st.location::geometry) AS longitude,
        st.country,
        st.region,
        se.se_id,
        se.title AS sensor_title,
        se.type AS sensor_type,
        se.category,
        se.unit,
        sf.date,
        sf.csv_url,
        sf.size_mb
    FROM sensor_files sf
    JOIN sensors se ON sf.se_id = se.se_id
    JOIN stations st ON sf.st_id = st.st_id
    WHERE sf.date BETWEEN %s AND %s
      AND {where_clause}
    ORDER BY st.country, st.region, se.category, st.st_id, se.se_id, sf.date;
    """
 
    cursor.execute(download_query, (effective_from, effective_to, *params))
    rows = cursor.fetchall()
    conn.close()
 
    if not rows:
        return {"message": "No data found for the given filters."}
 
    total_size_mb = sum(r[15] or 0 for r in rows)
    total_files = len(rows)
    label = f"{country}_{region}" if country and region else country or "all"
 
    # Build urls.txt (TSV) in memory
    tsv_buffer = io.StringIO()
    tsv_writer = csv.writer(tsv_buffer, delimiter="\t")
    tsv_writer.writerow([
        "st_id", "station_name", "exposure", "model",
        "latitude", "longitude", "country", "region",
        "se_id", "sensor_title", "sensor_type", "category", "unit",
        "date", "csv_url", "size_mb"
    ])
    for r in rows:
        tsv_writer.writerow([
            r[0], r[1], r[2], r[3],
            r[4], r[5], r[6], r[7],
            r[8], r[9], r[10], r[11], r[12],
            r[13], r[14], r[15] or 0
        ])
    urls_txt = tsv_buffer.getvalue().encode("utf-8")
 
    # Read downloader files from the ./downloader folder 
    downloader_dir = Path(__file__).parent / "downloader"
    downloader_py = (downloader_dir / "osem_downloader.py").read_bytes()
    requirements = (downloader_dir / "requirements.txt").read_bytes()
    readme = (downloader_dir / "README.md").read_bytes()
 
    # Pack everything into a zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("urls.txt", urls_txt)
        zf.writestr("osem_downloader.py", downloader_py)
        zf.writestr("requirements.txt", requirements)
        zf.writestr("README.md", readme)
    zip_buffer.seek(0)
 
    generated = datetime.today().strftime('%Y-%m-%d_%H-%M-%S')
 
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=osem_{label}_{generated}.zip",
            "X-Total-Files": str(total_files),
            "X-Total-Size-MB": f"{total_size_mb:.2f}"
        }
    )