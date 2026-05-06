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
    
    
print('done!')