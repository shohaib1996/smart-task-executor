from celery import Celery
from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "smart_task_executor",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.workflow_tasks"],
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=600,  # 10 minutes max
    task_soft_time_limit=540,  # 9 minutes soft limit
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Retry configuration
    task_default_retry_delay=60,
    task_max_retries=3,
)

# Task routes
celery_app.conf.task_routes = {
    "app.tasks.workflow_tasks.*": {"queue": "workflows"},
}
