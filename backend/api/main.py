from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from shapely import to_geojson

from functions.db_con import get_connection

app = FastAPI(title="openSenseMap Archive API")


# All Endpoints
@app.get("/", response_class=PlainTextResponse)
async def all_endpoints():
    """List all available endpoints."""
    lines = [f"This is the {app.title}", "", "Routes:"]
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        description = (route.endpoint.__doc__ or "").strip()
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            lines.append(f"{method:<7}{route.path:<45}{description}")
    return "\n".join(lines)


# Discovery
@app.get("/regions")
async def list_regions(country: str | None = Query(None)):
    conn = get_connection()
    try:
        with conn.cursor() as cur:

            if country:
                cur.execute(
                    """
                    SELECT r.region
                    FROM boxes b
                    INNER JOIN regions r ON b.region_id = r.id
                    WHERE r.country = %s
                    GROUP BY r.region
                    ORDER BY r.region
                    """,
                    (country,),
                )

                return {
                    "country": country,
                    "regions": [row[0] for row in cur.fetchall()]
                }

            cur.execute(
                """
                SELECT DISTINCT r.country, r.region
                FROM boxes b
                INNER JOIN regions r ON b.region_id = r.id
                GROUP BY r.country, r.region
                ORDER BY r.country, r.region
                """
            )

            countries: dict[str, list[str]] = {}

            for country_name, region_name in cur.fetchall():
                countries.setdefault(country_name, []).append(region_name)

            return {
                "countries": [
                    {"country": c, "regions": rs}
                    for c, rs in countries.items()
                ]
            }

    finally:
        conn.close()



@app.get("/boxes")
async def list_boxes(
    country: str | None = Query(None),
    region: str | None = Query(None),
    exposure: str | None = Query(None),
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
):

    if to_date and not from_date:
        raise HTTPException(
            status_code=400,
            detail="from_date is required when to_date is provided",
        )

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            query = """
                SELECT
                    b.id AS box_id,
                    b.name,
                    b.exposure,
                    b.model,
                    b.updated_at,

                    r.country,
                    r.region,

                    ST_X(b.location) AS lon,
                    ST_Y(b.location) AS lat,

                    COALESCE(
                        json_agg(
                            json_build_object(
                                '_id', s.id,
                                'boxes_id', s.box_id,
                                'sensorType', s.sensor_type,
                                'title', s.title,
                                'unit', s.unit
                            )
                        ) FILTER (WHERE s.id IS NOT NULL),
                        '[]'
                    ) AS sensors

                FROM boxes b
                INNER JOIN regions r ON r.id = b.region_id
                LEFT JOIN sensors s ON s.box_id = b.id
                WHERE 1=1
            """

            params = []

            if country:
                query += " AND r.country = %s"
                params.append(country)

            if region:
                query += " AND r.region = %s"
                params.append(region)

            if exposure:
                query += " AND b.exposure = %s"
                params.append(exposure)

            if phenomenon:
                query += " AND s.title = %s"
                params.append(phenomenon)

            if sensor_type:
                query += " AND s.sensor_type = %s"
                params.append(sensor_type)

            if from_date and to_date:
                query += """
                    AND EXISTS (
                        SELECT 1
                        FROM measurements mm
                        WHERE mm.sensor_id = s.id
                        AND mm.time >= %s
                        AND mm.time < %s::date + INTERVAL '1 day'
                    )
                """
                params.append(from_date)
                params.append(to_date)

            elif from_date:
                query += """
                    AND EXISTS (
                        SELECT 1
                        FROM measurements mm
                        WHERE mm.sensor_id = s.id
                        AND mm.time >= %s
                        AND mm.time < %s::date + INTERVAL '1 day'
                    )
                """
                params.append(from_date)
                params.append(from_date)

            query += """
                GROUP BY
                    b.id,
                    b.name,
                    b.exposure,
                    b.model,
                    b.updated_at,
                    r.country,
                    r.region,
                    b.location
                ORDER BY b.name
            """

            cur.execute(query, params)
            rows = cur.fetchall()

            result = [
                {
                    "_id": r[0],
                    "name": r[1],
                    "exposure": r[2],
                    "model": r[3],
                    "updatedAt": r[4].isoformat() if r[4] else None,

                    "country": r[5],
                    "region": r[6],

                    "currentLocation": {
                        "type": "Point",
                        "coordinates": [r[7], r[8]]
                    },

                    "sensors": r[9]
                }
                for r in rows
            ]

            return result

    finally:
        conn.close()



@app.get("/boxes/{box_id}")
async def get_box(box_id: str):
    pass


@app.get("/boxes/{box_id}/sensors")
async def get_box_sensors(box_id: str):
    pass


@app.get("/phenomena")
async def list_phenomena():
    pass



# Measurements
@app.get("/boxes/{box_id}/sensors/{sensor_id}/measurements")
async def get_sensor_measurements(
    box_id: str,
    sensor_id: str,
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    limit: int | None = Query(None),
):
    pass


# Exports
@app.post("/exports")
async def create_export(
    format: str = Query(...), 
    bbox: str | None = Query(None),
    country: str | None = Query(None),
    region: str | None = Query(None),
    box_id: str | None = Query(None), 
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    exposure: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    columns: str | None = Query(None),
):
    pass


@app.get("/exports/{job_id}")
async def get_export_status(job_id: str):
    pass


@app.get("/exports/{job_id}/download")
async def download_export(job_id: str):
    pass


@app.get("/exports")
async def list_exports(
    status: str | None = Query(None),
    limit: int | None = Query(None),
):
    pass


@app.delete("/exports/{job_id}")
async def delete_export(job_id: str):
    pass