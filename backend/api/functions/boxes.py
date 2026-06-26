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
AGGREGATE_BUCKET_INTERVALS = {
    "hourly": "1 hour",
    "daily": "1 day",
    "monthly": "1 month",
    "yearly": "1 year",
}


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


async def get_archive_summary() -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT stations, sensors, readings, countries, updated_at
                FROM summary
                ORDER BY summary_date DESC, updated_at DESC NULLS LAST
                LIMIT 1
                """
            )
            row = cur.fetchone()

    if not row:
        return {
            "total_stations": 0,
            "total_sensors": 0,
            "total_readings": 0,
            "total_countries": 0,
            "updated_at": None,
        }

    return {
        "total_stations": int(row[0] or 0),
        "total_sensors": int(row[1] or 0),
        "total_readings": int(row[2] or 0),
        "total_countries": int(row[3] or 0),
        "updated_at": row[4].isoformat() if row[4] else None,
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
            phenomenon_list = _split_csv(phenomenon)
            if phenomenon_list:
                query += " AND s.title = ANY(%s)"
                params.append(phenomenon_list)
            sensor_type_list = _split_csv(sensor_type)
            if sensor_type_list:
                query += " AND s.sensor_type = ANY(%s)"
                params.append(sensor_type_list)

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
            phenomenon_list = _split_csv(phenomenon)
            if phenomenon_list:
                query += " AND s.title = ANY(%s)"
                params.append(phenomenon_list)
            sensor_type_list = _split_csv(sensor_type)
            if sensor_type_list:
                query += " AND s.sensor_type = ANY(%s)"
                params.append(sensor_type_list)
            bucket_interval = AGGREGATE_BUCKET_INTERVALS[aggregate]
            if from_date:
                # Overlap test against the bucket's period, not just its start,
                # so e.g. a yearly bucket dated 2014-01-01 still matches a
                # requested range starting mid-year like 2014-06-01.
                query += f" AND agg.bucket + INTERVAL '{bucket_interval}' > %s"
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
            "measurementCount": int(r[15]) if r[15] is not None else None,
            "avgValue": float(r[16]) if r[16] is not None else None,
            "minValue": float(r[17]) if r[17] is not None else None,
            "maxValue": float(r[18]) if r[18] is not None else None,
        }
        for r in rows
    ]


async def query_measurements_raw(
    geometry_wkt: str | None,
    box_id: str | None = None,
    country: str | None = None,
    region: str | None = None,
    exposure: str | None = None,
    phenomenon: str | None = None,
    sensor_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Every individual measurement row (no bucketing/aggregation), for
    downloads. Distinct from query_boxes' "raw" box/sensor inventory listing
    used by list_boxes() for the map's station list - that one must keep
    returning box-level metadata only, since it backs every page load.
    """
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
                    s.id AS sensor_id,
                    s.sensor_type,
                    s.title,
                    s.unit,
                    m.time,
                    m.value
                FROM boxes b
                INNER JOIN regions r ON r.id = b.region_id
                INNER JOIN sensors s ON s.box_id = b.id
                INNER JOIN measurements m ON m.sensor_id = s.id
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
            phenomenon_list = _split_csv(phenomenon)
            if phenomenon_list:
                query += " AND s.title = ANY(%s)"
                params.append(phenomenon_list)
            sensor_type_list = _split_csv(sensor_type)
            if sensor_type_list:
                query += " AND s.sensor_type = ANY(%s)"
                params.append(sensor_type_list)
            if from_date:
                query += " AND m.time >= %s"
                params.append(from_date)
            if to_date:
                query += " AND m.time < %s::date + INTERVAL '1 day'"
                params.append(to_date)

            query += """
                ORDER BY b.name, s.title, m.time
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
            "aggregate": "raw",
            "time": r[13].isoformat() if r[13] else None,
            "value": float(r[14]) if r[14] is not None else None,
        }
        for r in rows
    ]


async def query_boxes_for_aggregate(
    *,
    aggregate: str,
    geometry_wkt: str | None,
    box_id: str | None = None,
    country: str | None = None,
    region: str | None = None,
    exposure: str | None = None,
    phenomenon: str | None = None,
    sensor_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Dispatches to a true per-measurement query for "raw" downloads, or the
    bucketed continuous-aggregate query otherwise. Used by the download/export
    paths only - list_boxes() (the map's station list) calls
    query_boxes_aggregated directly and is unaffected by this.
    """
    if aggregate == "raw":
        return await query_measurements_raw(
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

    return await query_boxes_aggregated(
        geometry_wkt=geometry_wkt,
        box_id=box_id,
        country=country,
        region=region,
        exposure=exposure,
        phenomenon=phenomenon,
        sensor_type=sensor_type,
        from_date=from_date,
        to_date=to_date,
        aggregate=aggregate,
    )


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


async def list_boxes(box_id: str | None) -> dict:
    aggregate = "monthly" if box_id else "raw"

    rows = await query_boxes_aggregated(
        geometry_wkt=None,
        box_id=box_id,
        aggregate=aggregate,
    )
    summary = await get_archive_summary()

    return {
        "summary": summary,
        "boxes": rows,
    }
