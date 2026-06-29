from datetime import datetime
from typing import List, Optional

import psycopg2.extras
from fastapi import Form, HTTPException, Query, UploadFile

from api.functions.db_con import get_db_connection
from api.functions.redis_cache import cached_or_compute, make_cache_key
from api.functions.upload_aoi import load_geometry_from_upload_or_geometry


def split_csv_values(values):
    if not values:
        return None

    normalized = []
    for value in values:
        if not value:
            continue
        normalized.extend(part.strip() for part in value.split(",") if part.strip())

    return normalized or None


class BoxFilters:
    def __init__(
        self,
        from_date: datetime,
        to_date: datetime,
        box_type: Optional[str],
        exposure: Optional[str],
        phenomena: Optional[List[str]],
        tags: Optional[List[str]],
        phenomenon: Optional[str] = None,
        sensor_type: Optional[str] = None,
    ):
        self.from_date = from_date
        self.to_date = to_date
        self.box_type = box_type
        self.exposure = exposure
        self.phenomena = split_csv_values((phenomena or []) + [phenomenon, sensor_type])
        self.tags = split_csv_values(tags)


def box_filters_query(
    from_date: datetime,
    to_date: datetime,
    box_type: Optional[str] = None,
    exposure: Optional[str] = None,
    phenomena: Optional[List[str]] = Query(None),
    tags: Optional[List[str]] = Query(None),
    phenomenon: Optional[str] = Query(None),
    sensor_type: Optional[str] = Query(None),
) -> BoxFilters:
    return BoxFilters(from_date, to_date, box_type, exposure, phenomena, tags, phenomenon, sensor_type)


def box_filters_form(
    from_date: datetime = Form(...),
    to_date: datetime = Form(...),
    box_type: Optional[str] = Form(None),
    exposure: Optional[str] = Form(None),
    phenomena: Optional[List[str]] = Form(None),
    tags: Optional[List[str]] = Form(None),
    phenomenon: Optional[str] = Form(None),
    sensor_type: Optional[str] = Form(None),
) -> BoxFilters:
    return BoxFilters(from_date, to_date, box_type, exposure, phenomena, tags, phenomenon, sensor_type)


def build_box_filters(filters: BoxFilters):
    conditions = []
    params = []

    if filters.box_type:
        conditions.append("b.box_type = %s")
        params.append(filters.box_type)

    if filters.exposure:
        conditions.append("b.exposure = %s")
        params.append(filters.exposure)

    # Require at least one measurement in the requested time range.
    sensor_conditions = ["m.time BETWEEN %s AND %s", "s.box_id = b.id"]
    sensor_params: list = [filters.from_date, filters.to_date]

    if filters.phenomena:
        sensor_conditions.append("(s.sensor_type = ANY(%s) OR s.title = ANY(%s))")
        sensor_params.append(filters.phenomena)
        sensor_params.append(filters.phenomena)

    if filters.tags:
        sensor_conditions.append("s.title = ANY(%s)")
        sensor_params.append(filters.tags)

    sensor_where = " AND ".join(sensor_conditions)
    conditions.append(f"""
        EXISTS (
            SELECT 1 FROM measurements m
            JOIN sensors s ON s.id = m.sensor_id
            WHERE {sensor_where}
        )
    """)
    params.extend(sensor_params)

    agg_sensor_filters = []
    agg_params = []

    if filters.phenomena:
        agg_sensor_filters.append("(s.sensor_type = ANY(%s) OR s.title = ANY(%s))")
        agg_params.append(filters.phenomena)
        agg_params.append(filters.phenomena)

    if filters.tags:
        agg_sensor_filters.append("s.title = ANY(%s)")
        agg_params.append(filters.tags)

    agg_filter_clause = ""
    if agg_sensor_filters:
        agg_filter_clause = "AND " + " AND ".join(agg_sensor_filters)

    return conditions, params, agg_filter_clause, agg_params


def format_box(row):
    return {
        "_id": row["box_id"],
        "name": row["box_name"],
        "sensors": row["sensors"],
        "exposure": row["exposure"],
        "createdAt": row["box_created_at"],
        "model": row["model"],
        "currentLocation": {
            "coordinates": [row["longitude"], row["latitude"]],
            "type": "Point",
            "timestamp": row["box_updated_at"],
        },
        "lastMeasurementAt": row["last_measurement_at"],
        "updatedAt": row["box_updated_at"],
        "boxType": row["box_type"],
        "country": row["country"],
        "region": row["region"],
    }


def fetch_boxes_with_query(where_clause: str, agg_filter_clause: str, agg_params: list, params: list):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(f"""
                SELECT
                    b.id AS box_id,
                    b.name AS box_name,
                    b.box_type,
                    b.exposure,
                    b.model,
                    b.created_at AS box_created_at,
                    b.updated_at AS box_updated_at,
                    ST_X(b.location) AS longitude,
                    ST_Y(b.location) AS latitude,
                    r.country,
                    r.region,
                    NULL AS last_measurement_at,
                    COALESCE(
                        json_agg(
                            json_build_object(
                                '_id', s.id,
                                '__v', 0,
                                'boxes_id', b.id,
                                'lastMeasurement', NULL,
                                'sensorType', s.sensor_type,
                                'title', s.title,
                                'unit', s.unit,
                                'measurements', '[]'::json
                            )
                        ) FILTER (WHERE s.id IS NOT NULL {agg_filter_clause}),
                        '[]'
                    ) AS sensors
                FROM boxes b
                JOIN regions r ON b.region_id = r.id
                LEFT JOIN sensors s ON b.id = s.box_id
                WHERE {where_clause}
                GROUP BY b.id, r.country, r.region
                ORDER BY b.id;
            """, agg_params + params)
            rows = cursor.fetchall()

    return [format_box(row) for row in rows]


def list_boxes():
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT
                        b.id AS box_id,
                        b.name AS box_name,
                        b.box_type,
                        b.exposure,
                        b.model,
                        b.created_at AS box_created_at,
                        b.updated_at AS box_updated_at,
                        ST_X(b.location) AS longitude,
                        ST_Y(b.location) AS latitude,
                        r.country,
                        r.region,
                        NULL AS last_measurement_at,
                        COALESCE(
                            json_agg(
                                json_build_object(
                                    '_id', s.id,
                                    '__v', 0,
                                    'boxes_id', b.id,
                                    'lastMeasurement', NULL,
                                    'sensorType', s.sensor_type,
                                    'title', s.title,
                                    'unit', s.unit,
                                    'measurements', '[]'::json
                                )
                            ) FILTER (WHERE s.id IS NOT NULL),
                            '[]'
                        ) AS sensors
                    FROM boxes b
                    JOIN regions r ON b.region_id = r.id
                    LEFT JOIN sensors s ON b.id = s.box_id
                    GROUP BY b.id, r.country, r.region
                    ORDER BY b.id;
                """)
                rows = cursor.fetchall()

        return [format_box(row) for row in rows]

    key = make_cache_key("boxes")
    return cached_or_compute(key, _compute, ttl=120)


def get_box(box_id: str):
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT
                        b.id AS box_id,
                        b.name AS box_name,
                        b.box_type,
                        b.exposure,
                        b.model,
                        b.created_at AS box_created_at,
                        b.updated_at AS box_updated_at,
                        ST_X(b.location) AS longitude,
                        ST_Y(b.location) AS latitude,
                        r.country,
                        r.region,
                        MAX(lm.time) AS last_measurement_at,
                        COALESCE(
                            json_agg(
                                json_build_object(
                                    '_id', s.id,
                                    '__v', 0,
                                    'boxes_id', b.id,
                                    'lastMeasurement',
                                        CASE WHEN lm.time IS NOT NULL THEN
                                            json_build_object('createdAt', lm.time, 'value', lm.value)
                                        ELSE NULL
                                        END,
                                    'sensorType', s.sensor_type,
                                    'title', s.title,
                                    'unit', s.unit,
                                    'measurements', '[]'::json
                                )
                            ) FILTER (WHERE s.id IS NOT NULL),
                            '[]'
                        ) AS sensors
                    FROM boxes b
                    JOIN regions r ON b.region_id = r.id
                    LEFT JOIN sensors s ON b.id = s.box_id
                    LEFT JOIN LATERAL (
                        SELECT m.time, m.value
                        FROM measurements m
                        WHERE m.sensor_id = s.id
                        ORDER BY m.time DESC
                        LIMIT 1
                    ) lm ON true
                    WHERE b.id = %s
                    GROUP BY b.id, r.country, r.region;
                """, (box_id,))
                row = cursor.fetchone()

        if row is None:
            return None

        return format_box(row)

    key = make_cache_key("box", box_id=box_id)
    result = cached_or_compute(key, _compute, ttl=120)

    if result is None:
        raise HTTPException(status_code=404, detail="Box not found")

    return result


def list_box_sensors(box_id: str):
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT
                        b.id AS box_id,
                        COALESCE(
                            json_agg(
                                json_build_object(
                                    '_id', s.id,
                                    '__v', 0,
                                    'boxes_id', b.id,
                                    'title', s.title,
                                    'unit', s.unit,
                                    'sensorType', s.sensor_type,
                                    'lastMeasurement',
                                        CASE WHEN lm.time IS NOT NULL THEN
                                            json_build_object('createdAt', lm.time, 'value', lm.value)
                                        ELSE NULL
                                        END,
                                    'measurements', '[]'::json
                                )
                            ) FILTER (WHERE s.id IS NOT NULL),
                            '[]'
                        ) AS sensors
                    FROM boxes b
                    LEFT JOIN sensors s ON b.id = s.box_id
                    LEFT JOIN LATERAL (
                        SELECT m.time, m.value
                        FROM measurements m
                        WHERE m.sensor_id = s.id
                        ORDER BY m.time DESC
                        LIMIT 1
                    ) lm ON true
                    WHERE b.id = %s
                    GROUP BY b.id;
                """, (box_id,))
                row = cursor.fetchone()

        if row is None:
            return None

        return {"_id": row["box_id"], "sensors": row["sensors"]}

    key = make_cache_key("box_sensors", box_id=box_id)
    result = cached_or_compute(key, _compute, ttl=120)

    if result is None:
        raise HTTPException(status_code=404, detail="Box not found")

    return result


def list_boxes_by_region(region: str, filters: BoxFilters):
    return list_boxes_by_country_region(None, region, filters)


def list_boxes_by_country_region(country: Optional[str], region: Optional[str], filters: BoxFilters):
    conditions, params, agg_filter_clause, agg_params = build_box_filters(filters)

    if country:
        conditions.append("r.country = %s")
        params.append(country)

    if region:
        conditions.append("r.region = %s")
        params.append(region)

    where_clause = " AND ".join(conditions)

    key = make_cache_key(
        "boxes_region",
        country=country,
        region=region,
        from_date=filters.from_date,
        to_date=filters.to_date,
        box_type=filters.box_type,
        exposure=filters.exposure,
        phenomena=filters.phenomena,
        tags=filters.tags,
    )
    results = cached_or_compute(
        key,
        lambda: fetch_boxes_with_query(where_clause, agg_filter_clause, agg_params, params),
        ttl=120,
    )

    if not results:
        raise HTTPException(status_code=404, detail="No boxes found for the given filters")

    return results


def list_boxes_by_aoi(
    area: Optional[UploadFile],
    geometry: Optional[str],
    filters: BoxFilters,
):
    area_wkt = load_geometry_from_upload_or_geometry(area, geometry)

    conditions, params, agg_filter_clause, agg_params = build_box_filters(filters)
    conditions.append("ST_Within(b.location, ST_GeomFromText(%s, 4326))")
    params.append(area_wkt)

    where_clause = " AND ".join(conditions)

    key = make_cache_key(
        "boxes_aoi",
        area_wkt=area_wkt,
        from_date=filters.from_date,
        to_date=filters.to_date,
        box_type=filters.box_type,
        exposure=filters.exposure,
        phenomena=filters.phenomena,
        tags=filters.tags,
    )
    results = cached_or_compute(
        key,
        lambda: fetch_boxes_with_query(where_clause, agg_filter_clause, agg_params, params),
        ttl=120,
    )

    if not results:
        raise HTTPException(status_code=404, detail="No boxes found for the given filters")

    return results
