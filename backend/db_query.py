from datetime import datetime
from functools import lru_cache

import json
import os
from connection import execute_query
from upload_file import process_kml, process_zip, process_geojson

# Both get_common_filters and get_boxes_by_aoi return one row per
# measurement with the box/sensor metadata repeated on every row (no
# aggregation, no pagination). Without a hard cap, a wide-enough area/date
# combination can ask Postgres for hundreds of millions of rows in one go --
# this happened during testing (EXPLAIN estimated ~313M rows for a whole-
# Germany bounding box over the archive's full date range) and killed the
# database connection. This LIMIT is a safety valve, not a paging
# mechanism: broad queries get silently truncated rather than crashing.
MAX_RAW_MEASUREMENT_ROWS = 100_000

# get_common_filters caps readings per matching sensor (via a LATERAL join)
# rather than relying on the global LIMIT alone, so a handful of
# high-frequency sensors can't consume the entire row budget and starve
# every other box out of the response. This is the default when a caller
# doesn't override limit_per_sensor; get_boxes_by_aoi passes 1, since it
# only wants each sensor's latest reading, not a history.
PER_SENSOR_MEASUREMENT_ROWS = 500

# Appends a filter clause for a single value or a list of values. "all"
# (the AOI selectors' default) and None both mean "no filter".
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


# Common filters. box_ids restricts to a pre-filtered set of boxes (e.g.
# the result of an AOI spatial lookup in get_boxes_by_aoi) -- this is the
# single place that turns box/sensor-level filters plus a date range into
# actual measurement rows, capped per sensor so no query here can blow up
# into hundreds of millions of rows (see MAX_RAW_MEASUREMENT_ROWS above).
def get_common_filters(
    exposure=None,
    tags=None,
    phenomenon=None,
    from_date=None,
    to_date=None,
    box_ids=None,
    limit_per_sensor=PER_SENSOR_MEASUREMENT_ROWS,
):
    if from_date or to_date:
        if not (from_date and to_date):
            raise ValueError("Error: Both from_date and to_date must be provided together.")

    # Filter box/sensor metadata first, then pull each matching sensor's
    # readings via a LATERAL join capped per sensor.
    query = '''
        WITH matching_sensors AS (
            SELECT
                b.id AS box_id,
                b.name,
                b.box_type,
                b.exposure,
                b.model,
                ST_Y(b.location) AS latitude,
                ST_X(b.location) AS longitude,
                s.id AS sensor_id,
                s.title,
                s.unit,
                s.sensor_type
            FROM boxes b
            INNER JOIN sensors s ON b.id = s.box_id
            WHERE s.last_measurement IS NOT NULL
              AND b.location IS NOT NULL
    '''

    parameters: list = []

    if box_ids:
        query += " AND b.id = ANY(%s)"
        parameters.append(list(box_ids))

    query, parameters = apply_filter(query, parameters, "b.exposure", exposure)
    query, parameters = apply_filter(query, parameters, "s.sensor_type", tags)
    query, parameters = apply_filter(query, parameters, "s.title", phenomenon)

    query += '''
        )
        SELECT
            ms.box_id, ms.name, ms.box_type, ms.exposure, ms.model,
            ms.latitude, ms.longitude,
            ms.sensor_id, ms.title, ms.unit, ms.sensor_type,
            m.time, m.value
        FROM matching_sensors ms
        JOIN LATERAL (
            SELECT time, value
            FROM measurements m
            WHERE m.sensor_id = ms.sensor_id
    '''

    if from_date and to_date:
        query += " AND m.time BETWEEN %s AND %s"
        parameters.append(from_date)
        parameters.append(to_date)

    query += '''
            ORDER BY m.time DESC
            LIMIT %s
        ) m ON true
        LIMIT %s
    '''
    parameters.append(limit_per_sensor)
    parameters.append(MAX_RAW_MEASUREMENT_ROWS)

    # Execute the fully built query, once, at the end
    rows = execute_query(query, tuple(parameters))

    return rows


# Region query. Cached: country/region assignment only changes when new
# boxes are ingested, not on every request.
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
    # Execute the SQL query to retrieve the summary data
    row = execute_query(''' SELECT 
                                stations, 
                                sensors, 
                                readings, 
                                countries 
                            FROM summary''')

    # Check if the query returned any results
    if not row:
        return None

    # Unpack the row into individual variables
    stations, sensors, readings, countries = row[0]

    # Return the summary data as a dictionary
    return {
        "stations": stations,
        "sensors": sensors,
        "readings": readings,
        "countries": countries
    }

def get_boxes(box_id: str = None):
 
    # Base query
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
            id, 
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
            last_timestamp
        ) = row
 
        if id not in boxes:
            boxes[id] = {
                "_id": id,
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
 
        boxes[id]["sensors"].append({
            "_id": sensor_id,
            "boxes_id": id,
            "lastMeasurement": {
                "value": last_measurement,
                "updatedAt": last_timestamp
            },
            "sensorType": sensor_type,
            "title": sensor_title,
            "unit": sensor_unit
        })
 
    return boxes.get(box_id) if box_id else list(boxes.values())

# Get Exposures. Cached: the set of exposure values in use changes only
# when boxes are ingested, not on every request.
@lru_cache(maxsize=1)
def get_exposures():
    rows = execute_query('''
        SELECT DISTINCT b.exposure
        FROM boxes b
        JOIN sensors s ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
            AND b.exposure IS NOT NULL
        ORDER BY b.exposure;
    ''')

    return {"exposures": [row[0] for row in rows]}

# Get Phenomenas. Cached, see get_exposures.
@lru_cache(maxsize=1)
def get_phenomena():
    rows = execute_query('''
        SELECT DISTINCT s.title
        FROM sensors s
        JOIN boxes b ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
        ORDER BY s.title;
    ''')

    return {"phenomena": [row[0][:1].upper() + row[0][1:] for row in rows]}

# Get Tags. Cached, see get_exposures.
@lru_cache(maxsize=1)
def get_tags():
    rows = execute_query('''
        SELECT DISTINCT s.sensor_type
        FROM sensors s
        JOIN boxes b ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
            AND s.sensor_type IS NOT NULL
        ORDER BY s.sensor_type;
    ''')

    return {"tags": [row[0][:1].upper() + row[0][1:] for row in rows]}

# Raw (unformatted) distinct filter values, exactly as stored in the DB.
# Used to build the AOI filter selectors: get_exposures/get_tags/get_phenomena
# capitalize their values for display, which would break an exact `= %s`
# filter match whenever the stored casing differs.
def get_filter_values():
    exposures = [row[0] for row in execute_query('''
        SELECT DISTINCT b.exposure
        FROM boxes b
        JOIN sensors s ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
            AND b.exposure IS NOT NULL
        ORDER BY b.exposure;
    ''') or []]

    tags = [row[0] for row in execute_query('''
        SELECT DISTINCT s.sensor_type
        FROM sensors s
        JOIN boxes b ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
            AND s.sensor_type IS NOT NULL
        ORDER BY s.sensor_type;
    ''') or []]

    phenomena = [row[0] for row in execute_query('''
        SELECT DISTINCT s.title
        FROM sensors s
        JOIN boxes b ON b.id = s.box_id
        WHERE s.last_measurement IS NOT NULL
            AND b.region_id IS NOT NULL
        ORDER BY s.title;
    ''') or []]

    return exposures, tags, phenomena

# Groups get_common_filters' flat (box_id, name, ..., sensor_id, ..., time,
# value) rows into the same box/sensor nesting get_boxes() returns, with
# each sensor's single latest reading in "lastMeasurement" -- matches
# get_common_filters being called with limit_per_sensor=1 (one row per
# sensor at most), not a full measurement history.
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

# Get Boxes by AOI. Runs the geospatial part first -- a standalone,
# index-backed lookup of which boxes fall inside the uploaded AOI -- then
# hands that box_id set to get_common_filters, which applies
# exposure/tags/phenomenon/date filtering. Only each sensor's single latest
# reading within the date range is returned, not a full history.
def get_boxes_by_aoi(
    geometry,
    from_date,
    to_date,
    exposure="all",
    tags="all",
    phenomenon="all"
):
    # File-extension to processor mapping for AOI uploads
    aoi_processors = {
        ".zip": process_zip,
        ".kml": process_kml,
        ".geojson": process_geojson,
    }

    ext = os.path.splitext(geometry)[1].lower()
    processor = aoi_processors.get(ext)

    if processor is None:
        raise ValueError(f"Error: Unsupported AOI file type '{ext}'.")

    # Dissolve/reproject the uploaded file into a single EPSG:4326 GeoJSON
    # geometry, then run the same spatial query for every file type
    aoi = processor(geometry)
    aoi_geometry = aoi["features"][0]["geometry"]

    # Geospatial part: which boxes fall inside the AOI? Cheap -- uses the
    # GIST index on boxes.location -- and returns at most a few thousand
    # ids, so it's fine as its own round trip before the heavier filtered
    # measurement fetch below.
    box_rows = execute_query('''
        SELECT b.id
        FROM boxes b
        WHERE b.location IS NOT NULL
          AND ST_Intersects(b.location, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
    ''', (json.dumps(aoi_geometry),))

    box_ids = [row[0] for row in box_rows or []]
    if not box_ids:
        return []

    # Then apply the same exposure/tags/phenomenon/date filtering
    # get_common_filters already implements, scoped to just those boxes.
    # Only the latest reading per sensor is needed here, not a history.
    rows = get_common_filters(
        exposure=exposure,
        tags=tags,
        phenomenon=phenomenon,
        from_date=from_date,
        to_date=to_date,
        box_ids=box_ids,
        limit_per_sensor=1,
    )

    return format_last_measurement_rows(rows)