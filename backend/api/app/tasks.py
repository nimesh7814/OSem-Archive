import os

try:
    from .bucket import upload_bytes
    from .celery_app import celery_app
    from .export import (
        aoi_measurement_export_filename,
        build_measurement_export,
        EXPORT_CONTENT_TYPES,
        measurement_export_filename,
    )
    from .measurements import get_aoi_measurement_rows, get_region_measurement_rows, TRUNCATION_NOTE
    from .schema import CommonMeasurementFilters
except ImportError:
    from bucket import upload_bytes
    from celery_app import celery_app
    from export import (
        aoi_measurement_export_filename,
        build_measurement_export,
        EXPORT_CONTENT_TYPES,
        measurement_export_filename,
    )
    from measurements import get_aoi_measurement_rows, get_region_measurement_rows, TRUNCATION_NOTE
    from schema import CommonMeasurementFilters

JOB_TRY = int(os.getenv("JOB_TRY", "3"))
JOB_RETRY_DELAY_SECONDS = 30


def build_filters(filters_data):
    return CommonMeasurementFilters(**{
        "from": filters_data.get("from"),
        "to": filters_data.get("to"),
        "tags": filters_data.get("tags"),
        "phenomena": filters_data.get("phenomena"),
        "exposure": filters_data.get("exposure"),
        "aggregate": filters_data.get("aggregate", "daily"),
    })


@celery_app.task(bind=True, name="exports.region_measurements")
def export_region_measurements(self, country, region, filters_data, file_format):
    try:
        self.update_state(state="PROGRESS", meta={"message": "Querying measurements."})

        filters = build_filters(filters_data)
        rows, truncated = get_region_measurement_rows(country, region, filters)
        filename = measurement_export_filename(country, region, file_format, filters.aggregate)

        self.update_state(state="PROGRESS", meta={"message": "Building export file.", "rows": len(rows)})

        content = build_measurement_export(rows, file_format).encode("utf-8")
        content_type = EXPORT_CONTENT_TYPES[file_format]

        object_name = f"exports/{self.request.id}/{filename}"

        self.update_state(state="PROGRESS", meta={"message": "Uploading export file.", "rows": len(rows)})
        upload_result = upload_bytes(object_name, content, content_type)

        result = {
            "filename": filename,
            "format": file_format,
            "aggregate": filters.aggregate,
            "rows": len(rows),
            "truncated": truncated,
            **upload_result,
        }
        if truncated:
            result["note"] = TRUNCATION_NOTE
        return result
    except Exception as exc:
        # Linear backoff: 30s, 60s, 90s, then give up and let the failure surface.
        retry_number = self.request.retries + 1
        if self.request.retries < JOB_TRY:
            countdown = JOB_RETRY_DELAY_SECONDS * retry_number
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=JOB_TRY,
            )
        raise


@celery_app.task(bind=True, name="exports.aoi_measurements")
def export_aoi_measurements(self, aoi_name, geometry, filters_data, file_format):
    try:
        self.update_state(state="PROGRESS", meta={"message": "Querying AOI measurements."})

        filters = build_filters(filters_data)
        rows, truncated = get_aoi_measurement_rows(geometry, filters)
        filename = aoi_measurement_export_filename(aoi_name, file_format, filters.aggregate)

        self.update_state(state="PROGRESS", meta={"message": "Building export file.", "rows": len(rows)})

        content = build_measurement_export(rows, file_format).encode("utf-8")
        content_type = EXPORT_CONTENT_TYPES[file_format]
        object_name = f"exports/{self.request.id}/{filename}"

        self.update_state(state="PROGRESS", meta={"message": "Uploading export file.", "rows": len(rows)})
        upload_result = upload_bytes(object_name, content, content_type)

        result = {
            "filename": filename,
            "format": file_format,
            "aggregate": filters.aggregate,
            "rows": len(rows),
            "aoi": aoi_name,
            "truncated": truncated,
            **upload_result,
        }
        if truncated:
            result["note"] = TRUNCATION_NOTE
        return result
    except Exception as exc:
        # Linear backoff: 30s, 60s, 90s, then give up and let the failure surface.
        retry_number = self.request.retries + 1
        if self.request.retries < JOB_TRY:
            countdown = JOB_RETRY_DELAY_SECONDS * retry_number
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=JOB_TRY,
            )
        raise
