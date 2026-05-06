from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from database import get_db_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await get_db_pool()
    print("Database connected")
    yield
    await app.state.db.close()
    print("Database disconnected")

app = FastAPI(lifespan=lifespan)

# Define a root endpoint
@app.get("/")
async def root():
    return {"message": "Welcome to OSemArchive API!"}

# Define a health endpoint
@app.get("/health")
async def health():
    return {"status": "Calm Down and Code is Working!"}

# Define a endpoint for overall information
@app.get('/overall')
async def overall(request: Request):
    db = request.app.state.db
    async with db.acquire() as conn:
        summary_row = await conn.fetchrow("""
            SELECT
                COUNT(country) AS country_count,
                SUM(stations) AS total_stations,
                SUM(sensors) AS total_sensors
            FROM summary_table
            """)
        
        downloads_row = await conn.fetchrow("SELECT SUM(count) AS total_downloads FROM downloads")
    
    return {
        "countries": summary_row['country_count'],
        "stations": summary_row['total_stations'],
        "sensors": summary_row['total_sensors'],
        "downloads": downloads_row['total_downloads'] or 0
    }

@app.get('/top_ten')
async def top_ten(request: Request):
    db = request.app.state.db
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                st.country,
                SUM(st.stations)          AS stations,
                SUM(st.sensors)           AS sensors,
                COALESCE(SUM(d.count), 0) AS downloads
            FROM summary_table st
            LEFT JOIN downloads d ON d.country = st.country
            WHERE st.country IS NOT NULL
            GROUP BY st.country
            ORDER BY stations DESC
            LIMIT 10
        """)

    return [
        {
            "country":   row['country'],
            "stations":  row['stations'],
            "sensors":   row['sensors'],
            "downloads": row['downloads']
        }
        for row in rows
    ]
    
    
print('done!')