import os
import tempfile

try:
    from .bucket import upload_file
    from .celery_app import celery_app
    from .export import (
        aoi_measurement_export_filename,
        EXPORT_CONTENT_TYPES,
        measurement_export_filename,
        write_measurement_export,
    )
    from .db import DatabaseBusyError, DatabaseConnectionLostError
    from .measurements import MAX_EXPORT_ROWS, iter_aoi_measurement_rows, iter_region_measurement_rows, TRUNCATION_NOTE
    from .schema import CommonMeasurementFilters
except ImportError:
    from bucket import upload_file
    from celery_app import celery_app
    from export import (
        aoi_measurement_export_filename,
        EXPORT_CONTENT_TYPES,
        measurement_export_filename,
        write_measurement_export,
    )
    from db import DatabaseBusyError, DatabaseConnectionLostError
    from measurements import MAX_EXPORT_ROWS, iter_aoi_measurement_rows, iter_region_measurement_rows, TRUNCATION_NOTE
    from schema import CommonMeasurementFilters

JOB_TRY = int(os.getenv("JOB_TRY", "3"))
JOB_RETRY_DELAY_SECONDS = 30


TRANSIENT_EXPORT_ERRORS = (DatabaseBusyError, DatabaseConnectionLostError)


def build_filters(filters_data):
    return CommonMeasurementFilters(**{
        "from": filters_data.get("from"),
        "to": filters_data.get("to"),
        "tags": filters_data.get("tags"),
        "phenomena": filters_data.get("phenomena"),
        "exposure": filters_data.get("exposure"),
        "aggregate": filters_data.get("aggregate", "daily"),
    })


def write_export_to_tempfile(rows, file_format):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=f".{file_format}", delete=False) as temp_file:
        write_measurement_export(rows, file_format, temp_file)
        return temp_file.name


@celery_app.task(bind=True, name="exports.region_measurements")
def export_region_measurements(self, country, region, filters_data, file_format):
    try:
        self.update_state(state="PROGRESS", meta={"message": "Querying measurements."})

        filters = build_filters(filters_data)
        rows, truncated = collect_rows_from_stream(iter_region_measurement_rows(country, region, filters))
        filename = measurement_export_filename(country, region, file_format, filters.aggregate)

        self.update_state(state="PROGRESS", meta={"message": "Building export file.", "rows": len(rows)})

        temp_path = write_export_to_tempfile(rows, file_format)
        content_type = EXPORT_CONTENT_TYPES[file_format]

        object_name = f"exports/{self.request.id}/{filename}"

        self.update_state(state="PROGRESS", meta={"message": "Uploading export file.", "rows": len(rows)})
        try:
            with open(temp_path, "rb") as temp_file:
                upload_result = upload_file(object_name, temp_file, os.path.getsize(temp_path), content_type)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

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
    except TRANSIENT_EXPORT_ERRORS as exc:
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
    except Exception:
        raise


@celery_app.task(bind=True, name="exports.aoi_measurements")
def export_aoi_measurements(self, aoi_name, geometry, filters_data, file_format):
    try:
        self.update_state(state="PROGRESS", meta={"message": "Querying AOI measurements."})

        filters = build_filters(filters_data)
        rows, truncated = collect_rows_from_stream(iter_aoi_measurement_rows(geometry, filters))
        filename = aoi_measurement_export_filename(aoi_name, file_format, filters.aggregate)

        self.update_state(state="PROGRESS", meta={"message": "Building export file.", "rows": len(rows)})

        temp_path = write_export_to_tempfile(rows, file_format)
        content_type = EXPORT_CONTENT_TYPES[file_format]
        object_name = f"exports/{self.request.id}/{filename}"

        self.update_state(state="PROGRESS", meta={"message": "Uploading export file.", "rows": len(rows)})
        try:
            with open(temp_path, "rb") as temp_file:
                upload_result = upload_file(object_name, temp_file, os.path.getsize(temp_path), content_type)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

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
    except TRANSIENT_EXPORT_ERRORS as exc:
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
    except Exception:
        raise


def collect_rows_from_stream(row_iterable):
    rows = []
    for row in row_iterable:
        rows.append(row)
        if len(rows) > MAX_EXPORT_ROWS:
            return rows[:MAX_EXPORT_ROWS], True
    return rows, False
