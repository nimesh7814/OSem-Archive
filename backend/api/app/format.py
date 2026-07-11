import math
from datetime import timezone


def format_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def format_box(row):
    current_location = None
    if row["longitude"] is not None and row["latitude"] is not None:
        current_location = {
            "coordinates": [row["longitude"], row["latitude"]],
            "type": "Point",
            "timestamp": format_datetime(row["last_measurement_at"] or row["updated_at"]),
        }

    box = {
        "_id": row["id"],
        "name": row["name"],
        "sensors": row["sensors"] or [],
        "exposure": row["exposure"] or "unknown",
        "createdAt": format_datetime(row["created_at"]),
        "model": row["model"] or "custom",
        "country": row["country"],
        "region": row["region"],
        "currentLocation": current_location,
        "updatedAt": format_datetime(row["updated_at"]),
    }

    last_measurement_at = format_datetime(row["last_measurement_at"])
    if last_measurement_at is not None:
        box["lastMeasurementAt"] = last_measurement_at

    return box


def format_measurement_value(value):
    if value is None:
        return None
    measurement = float(value)
    if not math.isfinite(measurement):
        return None
    return measurement


def format_measurement_boxes(rows):
    boxes = {}

    for row in rows:
        box_id = row["box_id"]
        sensor_id = row["sensor_id"]

        if box_id not in boxes:
            current_location = None
            if row["longitude"] is not None and row["latitude"] is not None:
                current_location = {
                    "coordinates": [row["longitude"], row["latitude"]],
                    "type": "Point",
                    "timestamp": format_datetime(row["last_measurement_at"] or row["updated_at"]),
                }

            boxes[box_id] = {
                "_id": box_id,
                "name": row["name"],
                "sensors": {},
                "exposure": row["exposure"] or "unknown",
                "createdAt": format_datetime(row["created_at"]),
                "model": row["model"] or "custom",
                "country": row["country"],
                "region": row["region"],
                "currentLocation": current_location,
                "updatedAt": format_datetime(row["updated_at"]),
            }

            last_measurement_at = format_datetime(row["last_measurement_at"])
            if last_measurement_at is not None:
                boxes[box_id]["lastMeasurementAt"] = last_measurement_at

        sensors = boxes[box_id]["sensors"]
        if sensor_id not in sensors:
            sensors[sensor_id] = {
                "_id": sensor_id,
                "boxes_id": box_id,
                "sensorType": row["sensor_type"],
                "title": row["title"],
                "unit": row["unit"],
                "measurements": [],
            }

        measurement = {
            "time": format_datetime(row["bucket"]),
        }
        if row["aggregate"] == "raw":
            measurement["value"] = format_measurement_value(row["value"])
        else:
            measurement.update({
                "avgValue": format_measurement_value(row["avg_value"]),
                "minValue": format_measurement_value(row["min_value"]),
                "maxValue": format_measurement_value(row["max_value"]),
                "readings": int(row["rdgs_count"]),
            })

        sensors[sensor_id]["measurements"].append(measurement)

    results = list(boxes.values())
    for box in results:
        box["sensors"] = list(box["sensors"].values())

    return results
