import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from celery import Celery
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
result_backend = os.getenv("CELERY_RESULT_BACKEND", broker_url)

celery_app = Celery(
    "osem_exports",
    broker=broker_url,
    backend=result_backend,
)

celery_app.conf.update(
    accept_content=["json"],
    result_expires=int(os.getenv("MINIO_TTL_SECONDS", 86400)),
    result_serializer="json",
    task_serializer="json",
    task_track_started=True,
    timezone="UTC",
    # Bounds how long send_task()/AsyncResult() can block if the broker is unreachable.
    broker_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    },
    broker_connection_timeout=5,
    # Not a full guarantee on its own (see EXPORT_QUEUE_TIMEOUT_SECONDS below for the hard wall-clock backstop).
    broker_connection_retry=True,
    broker_connection_max_retries=1,
    # Same timeout reasoning, for reading job status/results back out.
    result_backend_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    },
)


class ExportQueueError(Exception):
    pass


EXPORT_QUEUE_TIMEOUT_SECONDS = float(os.getenv("EXPORT_QUEUE_TIMEOUT_SECONDS", "8"))

# Runs Celery calls on a thread so a stuck broker can't block the HTTP request past EXPORT_QUEUE_TIMEOUT_SECONDS.
_queue_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="export-queue")


def _call_with_timeout(func, *args, **kwargs):
    future = _queue_executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=EXPORT_QUEUE_TIMEOUT_SECONDS)
    except FutureTimeoutError as exc:
        raise ExportQueueError("The export queue is temporarily unavailable. Please try again shortly.") from exc
    except Exception as exc:
        raise ExportQueueError("The export queue is temporarily unavailable. Please try again shortly.") from exc


def send_export_task(name, kwargs):
    return _call_with_timeout(celery_app.send_task, name, kwargs=kwargs, retry=False)


def get_export_result(job_id):
    result = celery_app.AsyncResult(job_id)
    # Touching .state triggers the backend lookup, so a dead result backend raises here instead of later.
    _call_with_timeout(lambda: result.state)
    return result


def export_workers_available():
    inspect = celery_app.control.inspect(timeout=2)
    workers = _call_with_timeout(lambda: inspect.ping() or {})
    return bool(workers)


def ensure_export_workers_available():
    if not export_workers_available():
        raise ExportQueueError("No export worker is currently available. Please try again shortly.")
