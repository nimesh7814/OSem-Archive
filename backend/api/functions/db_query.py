from datetime import datetime
from functools import lru_cache

import json
import os
from connection import execute_query
from upload_file import process_kml, process_zip, process_geojson

# Safety cap on rows from get_common_filters (one row per sensor, ~10-11k today).
MAX_RAW_MEASUREMENT_ROWS = 100_000

# "all" (the default) and None both mean "no filter".
def apply_filter(query, parameters, column, value):
    if value is None or value == "all":
        return query, parameters

    if isinstance(value, (list, tuple, set)):
        query += f" AND {column} = ANY(%s)"
        parameters.append(list(value))
    else:
        query += f" AND {column} = %s"
        parameters.append(value)

    return query, parameters


# from_date/to_date only gate whether a sensor had a reading in that window.
def get_common_filters(
    exposure=None,
    tags=None,
    phenomenon=None,
    from_date=None,
    to_date=None,
    box_ids=None,
):
    if from_date or to_date:
        if not (from_date and to_date):
            raise ValueError("Both from_date and to_date must be provided together.")

    query = '''
        SELECT
            b.id, b.name, b.box_type, b.exposure, b.model,
            ST_Y(b.location) AS latitude, ST_X(b.location) AS longitude,
            s.id, s.title, s.unit, s.sensor_type,
            s.last_timestamp, s.last_measurement
        FROM boxes b
        INNER JOIN sensors s ON b.id = s.box_id
    '''

    parameters: list = []

    if from_date and to_date:
        query += '''
            JOIN LATERAL (
                SELECT 1
                FROM measurements m
                WHERE m.sensor_id = s.id
                  AND m.time BETWEEN %s AND %s
                LIMIT 1
            ) qualifies ON true
        '''
        parameters.append(from_date)
        parameters.append(to_date)

    query += '''
        WHERE s.last_measurement IS NOT NULL
    '''

    if box_ids:
        query += " AND b.id = ANY(%s)"
        parameters.append(list(box_ids))

    query, parameters = apply_filter(query, parameters, "b.exposure", exposure)
    query, parameters = apply_filter(query, parameters, "s.sensor_type", tags)
    query, parameters = apply_filter(query, parameters, "s.title", phenomenon)

    query += " LIMIT %s"
    parameters.append(MAX_RAW_MEASUREMENT_ROWS)

    rows = execute_query(query, tuple(parameters))
    return rows


# Cached: country/region assignment rarely changes.
@lru_cache(maxsize=None)
def get_countries(country: str = None):
    if country:
        rows = execute_query('''
            SELECT DISTINCT r.country, r.region
            FROM regions r
            JOIN boxes b ON r.id = b.region_id
            JOIN sensors s ON b.id = s.box_id
            WHERE r.country = %s AND s.last_measurement IS NOT NULL
            ORDER BY r.country, r.region;
        ''', (country,))
    else:
        rows = execute_query('''
            SELECT DISTINCT r.country, r.region
            FROM regions r
            JOIN boxes b ON r.id = b.region_id
            JOIN sensors s ON b.id = s.box_id
            WHERE s.last_measurement IS NOT NULL
            ORDER BY r.country, r.region;
        ''')

    if not rows:
        return {}

    countries = {}
    for row in rows:
        country, region = row
        countries.setdefault(country, []).append(region)

    return countries

# Get summary data
def get_summary():
    row = execute_query("SELECT stations, sensors, readings, countries FROM summary")
    if not row:
        return None

    stations, sensors, readings, countries = row[0]
    return {
        "stations": stations,
        "sensors": sensors,
        "readings": readings,
        "countries": countries,
    }

def get_boxes(box_id: str = None):
    query = '''
        SELECT
            b.id,
            b.name,
            b.box_type,
            b.exposure,
            b.model,
            ST_Y(b.location) AS latitude,
            ST_X(b.location) AS longitude,
            r.country,
            r.region,
            s.id AS sensor_id,
            s.title,
            s.unit,
            s.sensor_type,
            s.last_measurement,
            s.last_timestamp
        FROM boxes b
        INNER JOIN sensors s ON b.id = s.box_id
        LEFT JOIN regions r ON b.region_id = r.id
        WHERE b.region_id IS NOT NULL AND s.last_measurement IS NOT NULL
    '''

    parameters = []

    if box_id:
        query += " AND b.id = %s"
        parameters.append(box_id)

    rows = execute_query(query, tuple(parameters))

    if not rows:
        return None if box_id else []

    boxes = {}

    for row in rows:
        (
            row_box_id,
            name,
            box_type,
            exposure,
            model,
            latitude,
            longitude,
            country,
            region,
            sensor_id,
            sensor_title,
            sensor_unit,
            sensor_type,
            last_measurement,
            last_timestamp,
        ) = row

        if row_box_id not in boxes:
            boxes[row_box_id] = {
                "_id": row_box_id,
                "name": name,
                "sensors": [],
                "exposure": exposure,
                "createdAt": datetime.now().astimezone(),
                "model": model,
                "country": country,
                "region": region,
                "currentLocation": {
                    "coordinates": [longitude, latitude],
                    "type": "Point"
                }
            }

        boxes[row_box_id]["sensors"].append({
            "_id": sensor_id,
            "boxes_id": row_box_id,
            "lastMeasurement": {
                "value": last_measurement,
                "updatedAt": last_timestamp
            },
            "sensorType": sensor_type,
            "title": sensor_title,
            "unit": sensor_unit
        })

    return boxes.get(box_id) if box_id else list(boxes.values())

# Shared by get_exposures/get_tags/get_phenomena/get_filter_values below.
def _distinct_column_values(column, require_not_null=True):
    not_null_clause = f"AND {column} IS NOT NULL" if require_not_null else ""
    rows = execute_query(f'''
        SELECT DISTINCT {column}
        FROM sensors s
        JOIN boxes b ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
            {not_null_clause}
        ORDER BY {column};
    ''')
    return [row[0] for row in rows or []]

# Capitalizes for display; get_filter_values keeps the raw casing for filter matches.
def _capitalize_first(value):
    return value[:1].upper() + value[1:]

@lru_cache(maxsize=1)
def get_exposures():
    return {"exposures": _distinct_column_values("b.exposure")}

@lru_cache(maxsize=1)
def get_phenomena():
    values = _distinct_column_values("s.title", require_not_null=False)
    return {"phenomena": [_capitalize_first(v) for v in values]}

@lru_cache(maxsize=1)
def get_tags():
    values = _distinct_column_values("s.sensor_type")
    return {"tags": [_capitalize_first(v) for v in values]}

# Raw, as-stored values used to build main.py's filter dropdowns.
def get_filter_values():
    exposures = _distinct_column_values("b.exposure")
    tags = _distinct_column_values("s.sensor_type")
    phenomena = _distinct_column_values("s.title", require_not_null=False)
    return exposures, tags, phenomena

# Groups get_common_filters' flat per-sensor rows into get_boxes()'s box/sensor nesting.
def format_last_measurement_rows(rows):
    boxes = {}

    for row in rows or []:
        (
            box_id, name, box_type, exposure, model,
            latitude, longitude,
            sensor_id, title, unit, sensor_type,
            time, value,
        ) = row

        if box_id not in boxes:
            boxes[box_id] = {
                "_id": box_id,
                "name": name,
                "boxType": box_type,
                "exposure": exposure,
                "model": model,
                "currentLocation": {
                    "coordinates": [longitude, latitude],
                    "type": "Point"
                },
                "sensors": [],
            }

        boxes[box_id]["sensors"].append({
            "_id": sensor_id,
            "boxes_id": box_id,
            "title": title,
            "unit": unit,
            "sensorType": sensor_type,
            "lastMeasurement": {
                "value": value,
                "updatedAt": time,
            },
        })

    return list(boxes.values())

# Resolves an uploaded AOI file to its box ids and dissolved EPSG:4326 geometry.
def get_box_ids_by_aoi(geometry):
    aoi_processors = {
        ".zip": process_zip,
        ".kml": process_kml,
        ".geojson": process_geojson,
    }

    ext = os.path.splitext(geometry)[1].lower()
    processor = aoi_processors.get(ext)

    if processor is None:
        raise ValueError(f"Unsupported AOI file type '{ext}'.")

    aoi = processor(geometry)
    aoi_geometry = aoi["features"][0]["geometry"]

    box_rows = execute_query('''
        SELECT b.id
        FROM boxes b
        WHERE b.location IS NOT NULL
          AND ST_Intersects(b.location, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
    ''', (json.dumps(aoi_geometry),))

    return [row[0] for row in box_rows or []], aoi_geometry

# Get Boxes by AOI: find boxes inside the uploaded AOI, then filter via get_common_filters.
def get_boxes_by_aoi(
    geometry,
    from_date,
    to_date,
    exposure="all",
    tags="all",
    phenomenon="all"
):
    box_ids, aoi_geometry = get_box_ids_by_aoi(geometry)
    boxes = []

    if box_ids:
        rows = get_common_filters(
            exposure=exposure,
            tags=tags,
            phenomenon=phenomenon,
            from_date=from_date,
            to_date=to_date,
            box_ids=box_ids,
        )
        boxes = format_last_measurement_rows(rows)

    return {"boxes": boxes, "geometry": aoi_geometry}

# Resolves a region name to its box ids and dissolved geometry, or (None, None) if unknown.
def get_box_ids_by_region(region):
    geometry_rows = execute_query('''
        SELECT ST_AsGeoJSON(ST_Union(r.geometry))
        FROM regions r
        WHERE r.region = %s
    ''', (region,))

    geometry_json = geometry_rows[0][0] if geometry_rows else None
    if not geometry_json:
        return None, None

    box_rows = execute_query('''
        SELECT b.id
        FROM boxes b
        JOIN regions r ON b.region_id = r.id
        WHERE r.region = %s
    ''', (region,))

    return [row[0] for row in box_rows or []], geometry_json

# geometry is None in the result when the region name doesn't match anything.
def get_boxes_by_region(
    region,
    from_date,
    to_date,
    exposure="all",
    tags="all",
    phenomenon="all"
):
    box_ids, geometry_json = get_box_ids_by_region(region)
    if geometry_json is None:
        return {"region": region, "boxes": [], "geometry": None}

    boxes = []

    if box_ids:
        rows = get_common_filters(
            exposure=exposure,
            tags=tags,
            phenomenon=phenomenon,
            from_date=from_date,
            to_date=to_date,
            box_ids=box_ids,
        )
        boxes = format_last_measurement_rows(rows)

    return {"region": region, "boxes": boxes, "geometry": json.loads(geometry_json)}