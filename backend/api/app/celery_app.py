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
    # Without these, a broker that is down or unreachable makes
    # send_task()/AsyncResult() block indefinitely instead of raising —
    # see the API's own request-handling code for how that's turned into
    # a fast 503 rather than a hung request.
    broker_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    },
    broker_connection_timeout=5,
    # Best-effort: ask kombu to not retry connecting more than once. In
    # testing this alone did not reliably bound the worst case (kombu's
    # retry/backoff behavior compounded with slow DNS resolution for an
    # unreachable host still took minutes), so it's backed by a hard
    # wall-clock timeout below — that's the actual guarantee.
    broker_connection_retry=True,
    broker_connection_max_retries=1,
    # Same reasoning, for reading job status/results back out.
    result_backend_transport_options={
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
    },
)


class ExportQueueError(Exception):
    pass


EXPORT_QUEUE_TIMEOUT_SECONDS = float(os.getenv("EXPORT_QUEUE_TIMEOUT_SECONDS", "8"))

# Celery/kombu's own retry and timeout settings (above) turned out not to
# reliably bound how long a call can block when the broker/result backend
# is unreachable. Running the call on a separate thread and giving up on
# *waiting* for it after EXPORT_QUEUE_TIMEOUT_SECONDS guarantees the HTTP
# request gets a fast, honest answer regardless of what Celery does
# internally. The abandoned thread is left to finish or die on its own;
# it holds no request state, so nothing leaks except the thread itself
# until it eventually completes.
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
    # .state is the property that actually triggers the backend lookup;
    # touch it here (under the same timeout) so a dead result backend
    # raises before the caller starts branching on job state.
    _call_with_timeout(lambda: result.state)
    return result


def export_workers_available():
    inspect = celery_app.control.inspect(timeout=2)
    workers = _call_with_timeout(lambda: inspect.ping() or {})
    return bool(workers)


def ensure_export_workers_available():
    if not export_workers_available():
        raise ExportQueueError("No export worker is currently available. Please try again shortly.")
