from dotenv import load_dotenv
from fastapi import FastAPI, Query, UploadFile, File, HTTPException, Body
from typing import Optional
import psycopg2
import os

load_dotenv()

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5436)),  # must be int
        database=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),  # now loaded
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
                COUNT(se.se_id) AS total_sensors,
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
            "latitude": r[1], 
            "longitude": r[2]} for r in rows]

    return result

