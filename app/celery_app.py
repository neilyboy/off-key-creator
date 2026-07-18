"""Celery application instance (Redis broker + result backend)."""
from celery import Celery

from .config import REDIS_URL

celery_app = Celery(
    "offkey",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,   # heavy tasks: one at a time
    task_acks_late=False,
    result_expires=86400,
    broker_connection_retry_on_startup=True,
)
