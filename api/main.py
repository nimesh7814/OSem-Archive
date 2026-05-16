from datetime import datetime
import io
import csv
import zipfile

from collections import defaultdict

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
    download: bool = Query(False, description="Set true to download CSVs as a zip")
):
    conn = get_db_connection()
    cursor = conn.cursor()

    effective_from = from_date or '2014-06-03'
    effective_to   = to_date   or datetime.today().strftime('%Y-%m-%d')

    selected_categories = None if sensor_type.lower() == "all" else [s.strip() for s in sensor_type.split(",")]

    # Build dynamic filters
    filters = ["1=1", "st.country IS NOT NULL", "sf.csv_url IS NOT NULL"]
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

    # Query sensor files with optional filters
    query = f"""
    SELECT
        st.country,
        st.region,
        st.st_id,
        st.name AS station_name,
        st.exposure,
        st.model,
        ST_Y(st.location::geometry) AS latitude,
        ST_X(st.location::geometry) AS longitude,
        se.se_id,
        se.title AS sensor_title,
        se.category,
        se.type AS sensor_type,
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

    cursor.execute(query, (effective_from, effective_to, *params))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return {"message": "No data found for the given filters."}

    # ------------------------------------------------------------------ #
    # Organize rows into folder structure
    # ------------------------------------------------------------------ #
    structure    = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list)))))
    sensor_meta  = {}
    station_meta = {}
    total_size_mb = 0

    summary = defaultdict(lambda: {"station_count": 0, "sensor_count": 0, "total_size_mb": 0})

    for row in rows:
        (
            country_val, region_val, st_id, station_name, exposure, model,
            lat, lon, se_id, sensor_title, category, s_type, unit,
            date, csv_url, size_mb
        ) = row

        structure[country_val][region_val][category][st_id][se_id].append({
            "date": date,
            "url": csv_url,
            "size_mb": size_mb or 0
        })

        station_meta[st_id] = {
            "st_id": st_id, "name": station_name, "exposure": exposure,
            "model": model, "latitude": lat, "longitude": lon,
            "country": country_val, "region": region_val
        }
        sensor_meta[se_id] = {
            "se_id": se_id, "st_id": st_id, "title": sensor_title,
            "category": category, "type": s_type, "unit": unit
        }

        # Aggregate summary
        key = (country_val, region_val)
        summary[key]["station_count"] = len({s["st_id"] for s in station_meta.values() if s["country"] == country_val and s["region"] == region_val})
        summary[key]["sensor_count"]  = len({s["se_id"] for s in sensor_meta.values() if station_meta[s["st_id"]]["country"] == country_val and station_meta[s["st_id"]]["region"] == region_val})
        summary[key]["total_size_mb"] += size_mb or 0
        total_size_mb += size_mb or 0

    summary_list = [
        {"country": k[0], "region": k[1], **v} for k, v in summary.items()
    ]

    if not download:
        return {"summary": summary_list}

    # ------------------------------------------------------------------ #
    # Download CSVs into a zip
    # ------------------------------------------------------------------ #
    base_dir = Path("downloads")
    base_dir.mkdir(exist_ok=True)

    for country_val, regions in structure.items():
        for region_val, categories in regions.items():
            for category_val, stations in categories.items():
                for st_id_val, sensors in stations.items():
                    for se_id_val, files in sensors.items():
                        folder = base_dir / country_val / region_val / category_val / st_id_val
                        folder.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            file_name = f"{se_id_val}_{f['date']}.csv"
                            file_path = folder / file_name
                            if not file_path.exists():
                                r = requests.get(f["url"], stream=True)
                                with open(file_path, "wb") as fd:
                                    for chunk in r.iter_content(chunk_size=1024*1024):
                                        fd.write(chunk)

    # Create zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in base_dir.rglob("*"):
            zf.write(file_path, file_path.relative_to(base_dir))
    zip_buffer.seek(0)

    label = f"{country}_{region}" if country and region else country or "all"
    generated = datetime.today().strftime('%Y-%m-%d_%H-%M-%S')

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=osem_{label}_{generated}.zip",
            "X-Total-Files": str(len(rows)),
            "X-Total-Size-MB": f"{total_size_mb:.2f}"
        }
    )