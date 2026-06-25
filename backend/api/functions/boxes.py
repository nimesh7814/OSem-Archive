from fastapi import Query
import psycopg2
from db_con import get_connection





# Function to list boxes with optional filters
async def list_boxes(
    bbox: str | None = Query(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    exposure: str | None = Query(None), 
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    format: str = Query("json"), 
):

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            query = """
                SELECT
                    b.id,
                    b.name,
                    b.box_type,
                    b.exposure,
                    b.model,
                    ST_AsText(b.location) AS geometry,
                    r.region,
                    r.country
                FROM boxes b
                INNER JOIN regions r ON b.region_id = r.id
                WHERE 1=1
            """

            params = []

            # Country filter
            if country:
                query += " AND r.country = %s"
                params.append(country)

            # Region filter
            if region:
                query += " AND r.region = %s"
                params.append(region)

            # Exposure filter
            if exposure:
                query += " AND b.exposure = %s"
                params.append(exposure)

            # Phenomenon filter
            if phenomenon:
                query += " AND b.title = %s"
                params.append(phenomenon)

            # Sensor type filter
            if sensor_type:
                query += " AND b.sensor_type = %s"
                params.append(sensor_type)

            # Bounding box filter (PostGIS)
            if bbox:
                # bbox format: minLon,minLat,maxLon,maxLat
                query += """
                    AND ST_Intersects(
                        b.location,
                        ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                    )
                """
                minx, miny, maxx, maxy = map(float, bbox.split(","))
                params.extend([minx, miny, maxx, maxy])

            query += " ORDER BY b.name"

            cur.execute(query, params)
            rows = cur.fetchall()

            result = [
                {
                    "id": r[0],
                    "name": r[1],
                    "box_type": r[2],
                    "exposure": r[3],
                    "model": r[4],
                    "geometry": r[5],
                    "region": r[6],
                    "country": r[7],
                }
                for r in rows
            ]

            return {"count": len(result), "data": result}

    finally:
        conn.close()