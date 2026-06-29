from celery import Celery

from api.functions.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND


celery_app = Celery(
    "opensensemap_export",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    result_expires=86400,
)

