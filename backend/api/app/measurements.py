import json
import os
from datetime import datetime, time, timezone

from fastapi import HTTPException

try:
    from .cache import get_cached, set_cached
    from .db import run_query, stream_query
    from .filters import validate_measurement_filters
    from .format import format_measurement_boxes
    from .measurement_cache import aoi_cache_key, get_cached_measurements, region_cache_key, set_cached_measurements
    from .queries import MEASUREMENT_AGGREGATES
except ImportError:
    from cache import get_cached, set_cached
    from db import run_query, stream_query
    from filters import validate_measurement_filters
    from format import format_measurement_boxes
    from measurement_cache import aoi_cache_key, get_cached_measurements, region_cache_key, set_cached_measurements
    from queries import MEASUREMENT_AGGREGATES


# Caps how many rows a single query can return, so a huge area/date range fails fast instead of exhausting memory.
MAX_EXPORT_ROWS = int(os.getenv("MAX_EXPORT_ROWS", "1000000"))

TRUNCATION_NOTE = (
    f"This request matched more than {MAX_EXPORT_ROWS:,} measurement records. Only the first {MAX_EXPORT_ROWS:,} "
    "(ordered by station and sensor, not date) were included - some stations in this area may be completely "
    "missing. Narrow the date range, area, or filters for full coverage."
)


def response_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_region_by_name(country, region):
    rows = run_query('''
        SELECT id
        FROM regions
        WHERE country = %s AND region = %s
        LIMIT 1
    ''', (country, region))

    if not rows:
        raise HTTPException(status_code=404, detail=f"Region '{region}' in '{country}' not found")

    return rows[0]


def get_validated_measurement_filters(country, region, filters):
    get_region_by_name(country, region)
    return validate_measurement_filters(filters)


def count_region_measurement_rows(country, region, filters):
    tag_filter, phenomenon_filter, exposure_filter = get_validated_measurement_filters(country, region, filters)
    aggregate = MEASUREMENT_AGGREGATES[filters.aggregate]
    measurement_table = aggregate["table"]
    time_column = aggregate["time_column"]

    query = f'''
        SELECT count(*) AS matched FROM (
            SELECT 1
            FROM {measurement_table} d
            JOIN sensors s ON s.id = d.sensor_id
            JOIN boxes b ON b.id = s.box_id
            JOIN regions r ON r.id = b.region_id
            WHERE r.country = %s
              AND r.region = %s
              AND s.last_measurement IS NOT NULL
    '''
    params = [country, region]

    if filters.from_date is not None and filters.to_date is not None:
        to_date_end = datetime.combine(filters.to_date, time.max)
        query += f'''
              AND d.{time_column} >= %s
              AND d.{time_column} <= %s
        '''
        params.extend([filters.from_date, to_date_end])

    if tag_filter is not None:
        query += " AND s.sensor_type = ANY(%s)"
        params.append(tag_filter)

    if phenomenon_filter is not None:
        query += " AND s.title = ANY(%s)"
        params.append(phenomenon_filter)

    if exposure_filter is not None:
        query += " AND b.exposure = ANY(%s)"
        params.append(exposure_filter)

    query += " LIMIT %s) matching_rows"
    params.append(MAX_EXPORT_ROWS + 1)

    return count_result(run_query(query, tuple(params)), filters.aggregate)


def count_aoi_measurement_rows(geometry, filters):
    tag_filter, phenomenon_filter, exposure_filter = validate_measurement_filters(filters)
    aggregate = MEASUREMENT_AGGREGATES[filters.aggregate]
    measurement_table = aggregate["table"]
    time_column = aggregate["time_column"]

    query = f'''
        SELECT count(*) AS matched FROM (
            SELECT 1
            FROM {measurement_table} d
            JOIN sensors s ON s.id = d.sensor_id
            JOIN boxes b ON b.id = s.box_id
            WHERE s.last_measurement IS NOT NULL
              AND b.location IS NOT NULL
              AND ST_Intersects(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), b.location)
    '''
    params = [json.dumps(geometry)]

    if filters.from_date is not None and filters.to_date is not None:
        to_date_end = datetime.combine(filters.to_date, time.max)
        query += f'''
              AND d.{time_column} >= %s
              AND d.{time_column} <= %s
        '''
        params.extend([filters.from_date, to_date_end])

    if tag_filter is not None:
        query += " AND s.sensor_type = ANY(%s)"
        params.append(tag_filter)

    if phenomenon_filter is not None:
        query += " AND s.title = ANY(%s)"
        params.append(phenomenon_filter)

    if exposure_filter is not None:
        query += " AND b.exposure = ANY(%s)"
        params.append(exposure_filter)

    query += " LIMIT %s) matching_rows"
    params.append(MAX_EXPORT_ROWS + 1)

    return count_result(run_query(query, tuple(params)), filters.aggregate)


def count_result(rows, aggregate):
    # The query is capped at MAX_EXPORT_ROWS + 1, so count is a lower bound (not the true total) once exceeded.
    matched = rows[0]["matched"]
    exceeds_limit = matched > MAX_EXPORT_ROWS
    return {
        "aggregate": aggregate,
        "count": min(matched, MAX_EXPORT_ROWS),
        "limit": MAX_EXPORT_ROWS,
        "exceedsLimit": exceeds_limit,
    }


def iter_region_measurement_rows(country, region, filters):
    tag_filter, phenomenon_filter, exposure_filter = get_validated_measurement_filters(country, region, filters)
    aggregate = MEASUREMENT_AGGREGATES[filters.aggregate]
    measurement_table = aggregate["table"]
    time_column = aggregate["time_column"]
    value_columns = aggregate["value_columns"]

    query = f'''
        SELECT
            %s AS aggregate,
            b.id AS box_id,
            b.name,
            b.exposure,
            b.model,
            b.created_at,
            b.updated_at,
            b.last_measurement_at,
            r.country,
            r.region,
            ST_X(b.location) AS longitude,
            ST_Y(b.location) AS latitude,
            s.id AS sensor_id,
            s.box_id,
            s.sensor_type,
            s.title,
            s.unit,
            {value_columns}
        FROM {measurement_table} d
        JOIN sensors s ON s.id = d.sensor_id
        JOIN boxes b ON b.id = s.box_id
        JOIN regions r ON r.id = b.region_id
        WHERE r.country = %s
          AND r.region = %s
          AND s.last_measurement IS NOT NULL
    '''
    params = [filters.aggregate, country, region]

    if filters.from_date is not None and filters.to_date is not None:
        to_date_end = datetime.combine(filters.to_date, time.max)
        query += f'''
          AND d.{time_column} >= %s
          AND d.{time_column} <= %s
        '''
        params.extend([filters.from_date, to_date_end])

    if tag_filter is not None:
        query += " AND s.sensor_type = ANY(%s)"
        params.append(tag_filter)

    if phenomenon_filter is not None:
        query += " AND s.title = ANY(%s)"
        params.append(phenomenon_filter)

    if exposure_filter is not None:
        query += " AND b.exposure = ANY(%s)"
        params.append(exposure_filter)

    query += f" ORDER BY b.id, s.id, d.{time_column} LIMIT %s"
    params.append(MAX_EXPORT_ROWS + 1)

    return stream_query(query, tuple(params))


def iter_aoi_measurement_rows(geometry, filters):
    tag_filter, phenomenon_filter, exposure_filter = validate_measurement_filters(filters)
    aggregate = MEASUREMENT_AGGREGATES[filters.aggregate]
    measurement_table = aggregate["table"]
    time_column = aggregate["time_column"]
    value_columns = aggregate["value_columns"]

    query = f'''
        SELECT
            %s AS aggregate,
            b.id AS box_id,
            b.name,
            b.exposure,
            b.model,
            b.created_at,
            b.updated_at,
            b.last_measurement_at,
            r.country,
            r.region,
            ST_X(b.location) AS longitude,
            ST_Y(b.location) AS latitude,
            s.id AS sensor_id,
            s.box_id,
            s.sensor_type,
            s.title,
            s.unit,
            {value_columns}
        FROM {measurement_table} d
        JOIN sensors s ON s.id = d.sensor_id
        JOIN boxes b ON b.id = s.box_id
        LEFT JOIN regions r ON r.id = b.region_id
        WHERE s.last_measurement IS NOT NULL
          AND b.location IS NOT NULL
          AND ST_Intersects(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), b.location)
    '''
    params = [filters.aggregate, json.dumps(geometry)]

    if filters.from_date is not None and filters.to_date is not None:
        to_date_end = datetime.combine(filters.to_date, time.max)
        query += f'''
          AND d.{time_column} >= %s
          AND d.{time_column} <= %s
        '''
        params.extend([filters.from_date, to_date_end])

    if tag_filter is not None:
        query += " AND s.sensor_type = ANY(%s)"
        params.append(tag_filter)

    if phenomenon_filter is not None:
        query += " AND s.title = ANY(%s)"
        params.append(phenomenon_filter)

    if exposure_filter is not None:
        query += " AND b.exposure = ANY(%s)"
        params.append(exposure_filter)

    query += f" ORDER BY b.id, s.id, d.{time_column} LIMIT %s"
    params.append(MAX_EXPORT_ROWS + 1)

    return stream_query(query, tuple(params))


def collect_limited_rows(row_iterable):
    rows = []
    for row in row_iterable:
        rows.append(row)
        if len(rows) > MAX_EXPORT_ROWS:
            return rows[:MAX_EXPORT_ROWS], True
    return rows, False


def get_aggregate_data_coverage(aggregate):
    cache_key = f"coverage:{aggregate}"
    cached_result = get_cached(cache_key)
    if cached_result is not None:
        return cached_result

    aggregate_config = MEASUREMENT_AGGREGATES[aggregate]
    rows = run_query(f'''
        SELECT
            min({aggregate_config["time_column"]}) AS min_time,
            max({aggregate_config["time_column"]}) AS max_time
        FROM {aggregate_config["table"]}
    ''')
    min_time = rows[0]["min_time"] if rows else None
    max_time = rows[0]["max_time"] if rows else None
    coverage = {
        "from": min_time.date().isoformat() if min_time else None,
        "to": max_time.date().isoformat() if max_time else None,
    }
    set_cached(cache_key, coverage)
    return coverage


def get_region_aoi(country, region):
    rows = run_query('''
        SELECT
            country,
            region,
            ST_AsGeoJSON(geometry)::json AS geometry,
            ST_Area(geometry::geography) / 1000000.0 AS area_sqkm
        FROM regions
        WHERE country = %s AND region = %s
        LIMIT 1
    ''', (country, region))

    if not rows:
        raise HTTPException(status_code=404, detail=f"Region '{region}' in '{country}' not found")

    row = rows[0]
    area_sqkm = row["area_sqkm"]
    return {
        "country": row["country"],
        "region": row["region"],
        "area_sqkm": round(float(area_sqkm), 6) if area_sqkm is not None else None,
        "geometry": row["geometry"],
    }


def coverage_note(no_data_scope, filters, rows):
    # Lets an empty result say "outside the archive's date range" instead of looking identical to "no sensors here".
    if rows:
        return None

    coverage = get_aggregate_data_coverage(filters.aggregate)
    if not (coverage["from"] and coverage["to"]):
        return None

    return (
        f"No '{filters.aggregate}' measurements were found {no_data_scope}. "
        f"Archived '{filters.aggregate}' data is available from {coverage['from']} to {coverage['to']}."
    )


def build_region_measurement_payload(country, region, filters, rows, truncated=False):
    payload = {
        "input": "region",
        "time": response_timestamp(),
        "aoi": get_region_aoi(country, region),
        "aggregate": filters.aggregate,
        "from": filters.from_date.isoformat() if filters.from_date is not None else None,
        "to": filters.to_date.isoformat() if filters.to_date is not None else None,
        "exposure": filters.exposure,
        "boxes": format_measurement_boxes(rows),
        "truncated": truncated,
    }

    if truncated:
        payload["note"] = TRUNCATION_NOTE
    else:
        note = coverage_note(f"for {country}/{region} in this date range", filters, rows)
        if note is not None:
            payload["note"] = note

    return payload


def build_aoi_measurement_payload(aoi, filters, rows, truncated=False):
    payload = {
        "input": "aoi",
        "time": response_timestamp(),
        "aoi": {
            "name": aoi["properties"]["input"],
            "area_sqkm": aoi["properties"]["area_sqkm"],
            "geometry": aoi["geometry"],
        },
        "aggregate": filters.aggregate,
        "from": filters.from_date.isoformat() if filters.from_date is not None else None,
        "to": filters.to_date.isoformat() if filters.to_date is not None else None,
        "exposure": filters.exposure,
        "boxes": format_measurement_boxes(rows),
        "truncated": truncated,
    }

    if truncated:
        payload["note"] = TRUNCATION_NOTE
    else:
        note = coverage_note("inside this AOI in this date range", filters, rows)
        if note is not None:
            payload["note"] = note

    return payload


def get_region_measurements_response(country, region, filters, count_aggregate=None):
    tag_filter, phenomenon_filter, exposure_filter = get_validated_measurement_filters(country, region, filters)
    cache_key = region_cache_key(country, region, filters, tag_filter, phenomenon_filter, exposure_filter)

    cached_payload = get_cached_measurements(cache_key)
    if cached_payload is not None:
        payload = dict(cached_payload)
        source = "minio"
    else:
        rows, truncated = collect_limited_rows(iter_region_measurement_rows(country, region, filters))
        payload = build_region_measurement_payload(country, region, filters, rows, truncated)
        set_cached_measurements(cache_key, payload)
        source = "database"

    if count_aggregate is not None:
        count_filters = filters.model_copy(update={"aggregate": count_aggregate})
        payload["recordCount"] = count_region_measurement_rows(country, region, count_filters)

    payload["source"] = source
    return payload


def get_aoi_measurements_response(aoi, filters, count_aggregate=None):
    tag_filter, phenomenon_filter, exposure_filter = validate_measurement_filters(filters)
    cache_key = aoi_cache_key(aoi["geometry"], filters, tag_filter, phenomenon_filter, exposure_filter)

    cached_payload = get_cached_measurements(cache_key)
    if cached_payload is not None:
        payload = dict(cached_payload)
        source = "minio"
    else:
        rows, truncated = collect_limited_rows(iter_aoi_measurement_rows(aoi["geometry"], filters))
        payload = build_aoi_measurement_payload(aoi, filters, rows, truncated)
        set_cached_measurements(cache_key, payload)
        source = "database"

    if count_aggregate is not None:
        count_filters = filters.model_copy(update={"aggregate": count_aggregate})
        payload["recordCount"] = count_aoi_measurement_rows(aoi["geometry"], count_filters)

    payload["source"] = source

    return payload
