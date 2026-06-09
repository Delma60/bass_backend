# backend/app/tasks/celery_app.py
from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "baas_platform",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.usage_sync",
        "app.tasks.invoice_gen",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Retry policy defaults
    task_max_retries=3,
    task_default_retry_delay=60,
    # Beat schedule — periodic tasks
    beat_schedule={
        "sync-usage-every-5-minutes": {
            "task": "app.tasks.usage_sync.sync_all_project_usage",
            "schedule": crontab(minute="*/5"),
        },
        "generate-monthly-invoices": {
            "task": "app.tasks.invoice_gen.generate_monthly_invoices",
            "schedule": crontab(hour=0, minute=0, day_of_month=1),  # 1st of each month
        },
        "flush-usage-counters-hourly": {
            "task": "app.tasks.usage_sync.flush_redis_counters",
            "schedule": crontab(minute=0),  # Every hour
        },
    },
)