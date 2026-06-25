"""
api/functions/boxes_export.py

query_boxes_aggregated — adds time-bucket aggregation on top of the base
box query when a date range + aggregate mode is given.

to_csv / to_geojson — serialize box rows to a downloadable file body.

_respond — returns either a streaming download Response or a plain JSON list.
"""

import csv
import io
import json

from fastapi import Response

from .db_con import get_db_connection

VALID_AGGREGATES = {"raw", "date", "month", "year"}
_BUCKET_SQL = {"date": "day", "month": "month", "year": "year"}


async def query_boxes_aggregated(
    geometry_wkt: str | None,
    box_id: str | None = None,
    country: str | None = None,
    region: str | None = None,
    exposure: str | None = None,
    phenomenon: str | None = None,
    sensor_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    aggregate: str = "raw",
) -> list[dict]:
    if aggregate not in VALID_AGGREGATES:
        raise ValueError(f"aggregate must be one of {VALID_AGGREGATES}")

    if aggregate == "raw" or not (from_date and to_date):
        from .boxes_search import query_boxes
        return await query_boxes(
            geometry_wkt=geometry_wkt,
            box_id=box_id,
            country=country,
            region=region,
            exposure=exposure,
            phenomenon=phenomenon,
            sensor_type=sensor_type,
            from_date=from_date,
            to_date=to_date,
        )

    bucket = _BUCKET_SQL[aggregate]

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            query = f"""
                SELECT
                    b.id AS box_id,
                    b.name,
                    b.exposure,
                    r.country,
                    r.region,
                    ST_X(b.location) AS lon,
                    ST_Y(b.location) AS lat,
                    date_trunc(%s, mm.time) AS bucket,
                    count(mm.value) AS measurement_count,
                    avg(mm.value)   AS avg_value
                FROM boxes b
                INNER JOIN regions r ON r.id = b.region_id
                INNER JOIN sensors s ON s.box_id = b.id
                INNER JOIN measurements mm ON mm.sensor_id = s.id
                WHERE mm.time >= %s
                  AND mm.time < %s::date + INTERVAL '1 day'
            """
            params: list = [bucket, from_date, to_date]

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

            query += """
                GROUP BY b.id, b.name, b.exposure, r.country, r.region, b.location, bucket
                ORDER BY b.name, bucket
            """

            cur.execute(query, params)
            rows = cur.fetchall()

    return [
        {
            "_id": r[0],
            "name": r[1],
            "exposure": r[2],
            "country": r[3],
            "region": r[4],
            "currentLocation": {"type": "Point", "coordinates": [r[5], r[6]]},
            "bucket": r[7].isoformat() if r[7] else None,
            "measurementCount": r[8],
            "avgValue": float(r[9]) if r[9] is not None else None,
        }
        for r in rows
    ]


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    flat_rows = []
    for r in rows:
        flat = dict(r)
        loc = flat.pop("currentLocation", None)
        if loc:
            flat["lon"] = loc["coordinates"][0]
            flat["lat"] = loc["coordinates"][1]
        if "sensors" in flat:
            flat["sensors"] = json.dumps(flat["sensors"])
        flat_rows.append(flat)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(flat_rows[0].keys()))
    writer.writeheader()
    writer.writerows(flat_rows)
    return buf.getvalue()


def to_geojson(rows: list[dict]) -> str:
    features = []
    for r in rows:
        loc = r.get("currentLocation")
        props = {k: v for k, v in r.items() if k != "currentLocation"}
        features.append({"type": "Feature", "geometry": loc, "properties": props})
    return json.dumps({"type": "FeatureCollection", "features": features})


def _respond(rows: list[dict], download: bool, file_type: str, base_filename: str):
    """Return a streaming download Response or a plain JSON list."""
    if not download:
        return rows
    if file_type == "csv":
        return Response(
            content=to_csv(rows),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={base_filename}.csv"},
        )
    return Response(
        content=to_geojson(rows),
        media_type="application/geo+json",
        headers={"Content-Disposition": f"attachment; filename={base_filename}.geojson"},
    )
