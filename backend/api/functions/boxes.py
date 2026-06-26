import csv
import io
import json
from dataclasses import dataclass

from fastapi import HTTPException, Query, Response

from .db_con import get_db_connection

VALID_AGGREGATES = {"raw", "hourly", "daily", "monthly", "yearly"}
AGGREGATE_TABLES = {
    "hourly": "reading_hourly",
    "daily": "reading_daily",
    "monthly": "reading_monthly",
    "yearly": "reading_yearly",
}


@dataclass(frozen=True)
class BoxQueryParams:
    exposure: str | None
    phenomenon: str | None
    sensor_type: str | None
    from_date: str | None
    to_date: str | None
    download: bool
    file_type: str
    aggregate: str


def box_query_params(
    exposure: str | None = Query(None),
    phenomenon: str | None = Query(None),
    sensor_type: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    download: bool = Query(False),
    file_type: str = Query("geojson"),
    aggregate: str = Query("hourly"),
) -> BoxQueryParams:
    if to_date and not from_date:
        raise HTTPException(400, "from_date is required when to_date is provided")
    if download and file_type not in ("geojson", "csv"):
        raise HTTPException(400, "file_type must be 'geojson' or 'csv'")
    if aggregate not in VALID_AGGREGATES:
        raise HTTPException(
            400,
            f"aggregate must be one of: {', '.join(sorted(VALID_AGGREGATES))}",
        )
    return BoxQueryParams(
        exposure=exposure,
        phenomenon=phenomenon,
        sensor_type=sensor_type,
        from_date=from_date,
        to_date=to_date,
        download=download,
        file_type=file_type,
        aggregate=aggregate,
    )


async def query_boxes(
    geometry_wkt: str | None = None,
    box_id: str | None = None,
    country: str | None = None,
    region: str | None = None,
    exposure: str | None = None,
    phenomenon: str | None = None,
    sensor_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
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
    aggregate: str = "hourly",
) -> list[dict]:
    if aggregate not in VALID_AGGREGATES:
        raise ValueError(f"aggregate must be one of: {', '.join(sorted(VALID_AGGREGATES))}")

    if aggregate == "raw":
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

    aggregate_table = AGGREGATE_TABLES[aggregate]

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            query = f"""
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
                    s.id AS sensor_id,
                    s.sensor_type,
                    s.title,
                    s.unit,
                    agg.bucket,
                    agg.sum_value,
                    agg.rdgs_count,
                    agg.avg_value,
                    agg.min_value,
                    agg.max_value
                FROM boxes b
                INNER JOIN regions r ON r.id = b.region_id
                INNER JOIN sensors s ON s.box_id = b.id
                INNER JOIN {aggregate_table} agg ON agg.sensor_id = s.id
                WHERE 1=1
            """
            params: list = []

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
            if from_date:
                query += " AND agg.bucket >= %s"
                params.append(from_date)
            if to_date:
                query += " AND agg.bucket < %s::date + INTERVAL '1 day'"
                params.append(to_date)

            query += """
                ORDER BY b.name, s.title, agg.bucket
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
            "currentLocation": {"type": "Point", "coordinates": [r[7], r[8]]},
            "sensor_id": r[9],
            "sensor_type": r[10],
            "sensor_title": r[11],
            "sensor_unit": r[12],
            "aggregate": aggregate,
            "bucket": r[13].isoformat() if r[13] else None,
            "sumValue": float(r[14]) if r[14] is not None else None,
            "measurementCount": r[15],
            "avgValue": float(r[16]) if r[16] is not None else None,
            "minValue": float(r[17]) if r[17] is not None else None,
            "maxValue": float(r[18]) if r[18] is not None else None,
        }
        for r in rows
    ]


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    flat_rows = []
    for r in rows:
        flat = {k: v for k, v in r.items() if k != "sensors"}
        loc = flat.pop("currentLocation", None)
        if loc:
            flat["lon"] = loc["coordinates"][0]
            flat["lat"] = loc["coordinates"][1]

        sensors = r.get("sensors")
        if not sensors:
            flat_rows.append(flat)
            continue

        for sensor in sensors:
            sensor_row = dict(flat)
            sensor_row["sensor_id"] = sensor.get("_id")
            sensor_row["sensor_box_id"] = sensor.get("boxes_id")
            sensor_row["sensor_type"] = sensor.get("sensorType")
            sensor_row["sensor_title"] = sensor.get("title")
            sensor_row["sensor_unit"] = sensor.get("unit")
            flat_rows.append(sensor_row)

    fieldnames = []
    for row in flat_rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
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


def respond(rows: list[dict], download: bool, file_type: str, base_filename: str):
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


async def list_boxes(box_id: str | None) -> list[dict]:
    return await query_boxes_aggregated(
        geometry_wkt=None,
        box_id=box_id,
        aggregate="raw",
    )
