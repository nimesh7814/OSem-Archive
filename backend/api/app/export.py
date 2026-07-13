import csv
import io
import json
import re

try:
    from .format import format_datetime
except ImportError:
    from format import format_datetime

MEASUREMENT_EXPORT_COLUMNS = [
    "aggregate",
    "country",
    "region",
    "box_id",
    "box_name",
    "exposure",
    "model",
    "longitude",
    "latitude",
    "sensor_id",
    "sensor_type",
    "phenomenon",
    "unit",
    "time",
    "value",
    "avg_value",
    "min_value",
    "max_value",
    "readings",
]

EXPORT_CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "geojson": "application/geo+json",
}


def safe_filename_part(value):
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return normalized or "export"


def measurement_export_filename(country, region, file_format, aggregate="daily"):
    country_part = safe_filename_part(country)
    region_part = safe_filename_part(region)
    aggregate_part = safe_filename_part(aggregate)
    return f"measurements-{country_part}-{region_part}-{aggregate_part}.{file_format}"


def aoi_measurement_export_filename(aoi_name, file_format, aggregate="daily"):
    aoi_part = safe_filename_part(aoi_name)
    aggregate_part = safe_filename_part(aggregate)
    return f"measurements-aoi-{aoi_part}-{aggregate_part}.{file_format}"


def export_value(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def build_measurement_csv(rows):
    output = io.StringIO()
    write_measurement_csv(rows, output)
    return output.getvalue()


def write_measurement_csv(rows, output):
    writer = csv.DictWriter(output, fieldnames=MEASUREMENT_EXPORT_COLUMNS)
    writer.writeheader()

    for row in rows:
        writer.writerow({
            "aggregate": row["aggregate"],
            "country": row["country"],
            "region": row["region"],
            "box_id": row["box_id"],
            "box_name": row["name"],
            "exposure": export_value(row["exposure"]),
            "model": export_value(row["model"]),
            "longitude": export_value(row["longitude"]),
            "latitude": export_value(row["latitude"]),
            "sensor_id": row["sensor_id"],
            "sensor_type": row["sensor_type"],
            "phenomenon": row["title"],
            "unit": row["unit"],
            "time": export_value(row["bucket"]),
            "value": export_value(row["value"]),
            "avg_value": export_value(row["avg_value"]),
            "min_value": export_value(row["min_value"]),
            "max_value": export_value(row["max_value"]),
            "readings": row["rdgs_count"],
        })


def measurement_record(row):
    measurement = {
        "time": export_value(row["bucket"]),
    }
    if row["aggregate"] == "raw":
        measurement["value"] = export_value(row["value"])
    else:
        measurement.update({
            "avgValue": export_value(row["avg_value"]),
            "minValue": export_value(row["min_value"]),
            "maxValue": export_value(row["max_value"]),
            "readings": int(row["rdgs_count"]),
        })
    return measurement


def build_measurement_geojson(rows):
    boxes = {}

    for row in rows:
        box_id = row["box_id"]
        sensor_id = row["sensor_id"]

        if box_id not in boxes:
            longitude = row["longitude"]
            latitude = row["latitude"]
            geometry = None
            if longitude is not None and latitude is not None:
                geometry = {
                    "type": "Point",
                    "coordinates": [longitude, latitude],
                }

            boxes[box_id] = {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "_id": box_id,
                    "name": row["name"],
                    "aggregate": row["aggregate"],
                    "country": row["country"],
                    "region": row["region"],
                    "exposure": row["exposure"] or "unknown",
                    "model": row["model"] or "custom",
                    "createdAt": format_datetime(row["created_at"]),
                    "updatedAt": format_datetime(row["updated_at"]),
                    "lastMeasurementAt": format_datetime(row["last_measurement_at"]),
                    "sensors": {},
                },
            }

        sensors = boxes[box_id]["properties"]["sensors"]
        if sensor_id not in sensors:
            sensors[sensor_id] = {
                "_id": sensor_id,
                "boxes_id": row["box_id"],
                "sensorType": row["sensor_type"],
                "title": row["title"],
                "unit": row["unit"],
                "measurements": [],
            }

        sensors[sensor_id]["measurements"].append(measurement_record(row))

    features = list(boxes.values())
    for feature in features:
        feature["properties"]["sensors"] = list(feature["properties"]["sensors"].values())

    return json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }, ensure_ascii=False)


def write_measurement_geojson(rows, output):
    output.write(build_measurement_geojson(rows))


def build_measurement_export(rows, file_format):
    if file_format == "csv":
        return build_measurement_csv(rows)
    if file_format == "geojson":
        return build_measurement_geojson(rows)
    raise ValueError(f"Unsupported export format: {file_format}")


def write_measurement_export(rows, file_format, output):
    if file_format == "csv":
        return write_measurement_csv(rows, output)
    if file_format == "geojson":
        return write_measurement_geojson(rows, output)
    raise ValueError(f"Unsupported export format: {file_format}")
