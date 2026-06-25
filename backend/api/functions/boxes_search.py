"""
api/functions/boxes_search.py

Shared box query used by GET /boxes, GET /boxes/region, POST /boxes/aoi.
"""

from .db_con import get_db_connection


async def query_boxes(
    geometry_wkt: str | None = None,
    box_id: str | None = None,  # comma-separated list of box IDs
    country: str | None = None,
    region: str | None = None,
    exposure: str | None = None,
    phenomenon: str | None = None,
    sensor_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
):
    with get_db_connection() as conn:
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

            if box_id:
                ids = [b.strip() for b in box_id.split(",") if b.strip()]
                query += " AND b.id = ANY(%s)"
                params.append(ids)

            if geometry_wkt:
                query += " AND ST_Intersects(b.location, ST_SetSRID(ST_GeomFromText(%s), 4326))"
                params.append(geometry_wkt)

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

            # date filter via EXISTS — measurements is only touched when a
            # date filter is actually requested, never on a plain listing
            if from_date and to_date:
                query += """
                    AND EXISTS (
                        SELECT 1 FROM measurements mm
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
                        SELECT 1 FROM measurements mm
                        WHERE mm.sensor_id = s.id
                          AND mm.time >= %s
                          AND mm.time < %s::date + INTERVAL '1 day'
                    )
                """
                params.append(from_date)
                params.append(from_date)

            query += """
                GROUP BY
                    b.id, b.name, b.exposure, b.model, b.updated_at,
                    r.country, r.region, b.location
                ORDER BY b.name
            """

            cur.execute(query, params)
            rows = cur.fetchall()

            return [
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
                        "coordinates": [r[7], r[8]],
                    },
                    "sensors": r[9],
                }
                for r in rows
            ]
