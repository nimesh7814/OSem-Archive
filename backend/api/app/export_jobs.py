try:
    from .bucket import check_object_storage_connection, get_presigned_download_url
    from .cache import set_cached, key_exists
    from .celery_app import ensure_export_workers_available, export_workers_available, get_export_result
except ImportError:
    from bucket import check_object_storage_connection, get_presigned_download_url
    from cache import set_cached, key_exists
    from celery_app import ensure_export_workers_available, export_workers_available, get_export_result

EXPORT_JOB_KEY_PREFIX = "export_job:"

STATUS_BY_CELERY_STATE = {
    "PENDING": "queued",
    "RECEIVED": "queued",
    "STARTED": "running",
    "RETRY": "retrying",
    "PROGRESS": "running",
}


def mark_export_job_submitted(job_id):
    # Distinguishes "queued job" from "job ID that was never submitted" for export_job_payload's 404 check.
    set_cached(f"{EXPORT_JOB_KEY_PREFIX}{job_id}", True)


def ensure_export_dependencies_available():
    check_object_storage_connection()
    ensure_export_workers_available()


def export_job_payload(job_id):
    if key_exists(f"{EXPORT_JOB_KEY_PREFIX}{job_id}") is False:
        return 404, {
            "error": {
                "code": 404,
                "message": f"No export job found for id '{job_id}'.",
            }
        }

    result = get_export_result(job_id)
    state = result.state
    meta = result.info if isinstance(result.info, dict) else {}

    if state == "SUCCESS":
        export_result = result.result if isinstance(result.result, dict) else {}
        object_name = export_result.get("objectName")
        download_url = get_presigned_download_url(object_name) if object_name else None

        return 200, {
            "jobId": job_id,
            "status": "completed",
            "export": {
                **export_result,
                "downloadUrl": download_url,
            },
        }

    if state == "FAILURE":
        return 500, {
            "error": {
                "code": 500,
                "message": "The export job failed.",
                "details": {
                    "jobId": job_id,
                    "reason": str(result.info),
                },
            }
        }

    status = STATUS_BY_CELERY_STATE.get(state, state.lower())
    if status in ("queued", "running", "retrying") and not export_workers_available():
        return 503, {
            "error": {
                "code": 503,
                "message": "No export worker is currently available. This job cannot progress until a worker is running.",
                "details": {
                    "jobId": job_id,
                    "status": status,
                },
            }
        }

    return 202, {
        "jobId": job_id,
        "status": status,
        "details": meta,
        "statusUrl": f"/exports/{job_id}",
    }
