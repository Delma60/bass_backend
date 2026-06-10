# backend/app/tasks/celery_app.py
from __future__ import annotations

import platform
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from celery import Celery
from celery.schedules import crontab

from app.config import settings


def _normalize_redis_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "rediss":
        return url

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    cert_reqs = query.get("ssl_cert_reqs")
    if cert_reqs is None or cert_reqs == "":
        query["ssl_cert_reqs"] = "required"
    else:
        normalized = cert_reqs.lower()
        if normalized.startswith("cert_"):
            normalized = normalized[5:]
        query["ssl_cert_reqs"] = normalized

    return urlunparse(parsed._replace(query=urlencode(query)))


celery_app = Celery(
    "baas_platform",
    broker=_normalize_redis_url(settings.redis_url),
    backend=_normalize_redis_url(settings.redis_url),
    include=[
        "app.tasks.usage_sync",
        "app.tasks.invoice_gen",
    ],
)

extra_conf = {}
if platform.system() == "Windows":
    extra_conf.update(
        worker_pool="solo",
        worker_concurrency=1,
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
    # Windows cannot safely use prefork pools; use solo mode locally.
    **extra_conf,
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