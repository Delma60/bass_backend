# backend/app/tasks/usage_sync.py
"""
Usage sync tasks.

The API layer increments lightweight Redis counters on each request via
record_usage() — which works both as a Celery task AND as a direct call.

Counter key format:  usage:{project_id}:{metric}

flush_redis_counters (hourly Celery task) reads and resets these counters,
writing aggregated rows into the usage_records Postgres table.

sync_all_project_usage (every 5 min) re-aggregates from usage_records
for the billing dashboard.
"""
import asyncio
import logging
from datetime import datetime, timezone

import redis as sync_redis

from app.config import settings
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

TRACKED_METRICS = [
    "db_reads",
    "db_writes",
    "nosql_reads",
    "nosql_writes",
    "storage_bytes",
    "function_calls",
    "ai_requests",
]

_redis_pool = sync_redis.ConnectionPool.from_url(settings.redis_url, decode_responses=True)


def _run_async(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _flush_counters_async() -> dict[str, int]:
    """Read + reset all Redis usage counters, write to Postgres."""
    import redis.asyncio as aioredis
    from sqlalchemy import text
    from app.db.postgres import AsyncSessionLocal

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    flushed: dict[str, int] = {}

    try:
        keys = []
        async for key in redis.scan_iter("usage:*"):
            keys.append(key)

        if not keys:
            return flushed

        # Read + reset atomically using pipeline
        pipe = redis.pipeline()
        for key in keys:
            pipe.getset(key, 0)  # returns old value, sets to 0
        values = await pipe.execute()

        # Group by project_id
        records: dict[str, dict[str, int]] = {}
        for key, raw_value in zip(keys, values):
            if not raw_value:
                continue
            parts = key.split(":", 2)
            if len(parts) != 3:
                continue
            _, project_id, metric = parts
            value = int(raw_value)
            if value <= 0:
                continue
            records.setdefault(project_id, {})[metric] = value

        # Write to Postgres
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            for project_id, metrics in records.items():
                for metric, value in metrics.items():
                    await session.execute(
                        text("""
                            INSERT INTO usage_records (id, project_id, metric, value, period_start, period_end)
                            VALUES (gen_random_uuid()::text, :project_id, :metric, :value, :period_start, :period_end)
                        """),
                        {
                            "project_id": project_id,
                            "metric": metric,
                            "value": value,
                            "period_start": now.replace(minute=0, second=0, microsecond=0),
                            "period_end": now,
                        },
                    )
                    flushed[f"{project_id}:{metric}"] = value
            await session.commit()

        logger.info("Flushed %d usage counter(s) to Postgres", len(flushed))
    finally:
        await redis.aclose()

    return flushed


async def _sync_usage_async() -> None:
    """Recompute current-period aggregates and cache in Redis for the dashboard."""
    import redis.asyncio as aioredis
    from sqlalchemy import text
    from app.db.postgres import AsyncSessionLocal

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT project_id, metric, SUM(value) AS total
                    FROM usage_records
                    WHERE period_start >= NOW() - INTERVAL '30 days'
                    GROUP BY project_id, metric
                """)
            )
            rows = result.mappings().all()

        pipe = redis.pipeline()
        for row in rows:
            cache_key = f"usage_agg:{row['project_id']}:{row['metric']}"
            pipe.setex(cache_key, 600, str(row["total"]))  # 10 min TTL
        await pipe.execute()

        logger.info("Synced %d usage aggregates to Redis cache", len(rows))
    finally:
        await redis.aclose()


@celery_app.task(name="app.tasks.usage_sync.flush_redis_counters", bind=True, max_retries=3)
def flush_redis_counters(self) -> dict:  # type: ignore[no-untyped-def]
    """Hourly: drain Redis usage counters → Postgres usage_records."""
    try:
        result = _run_async(_flush_counters_async())
        return {"status": "ok", "flushed": len(result)}
    except Exception as exc:
        logger.error("flush_redis_counters failed: %s", exc)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(name="app.tasks.usage_sync.sync_all_project_usage", bind=True, max_retries=3)
def sync_all_project_usage(self) -> dict:  # type: ignore[no-untyped-def]
    """Every 5 min: recompute 30-day aggregates into Redis for the dashboard."""
    try:
        _run_async(_sync_usage_async())
        return {"status": "ok"}
    except Exception as exc:
        logger.error("sync_all_project_usage failed: %s", exc)
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="app.tasks.usage_sync.record_usage", ignore_result=True)
def record_usage(project_id: str, metric: str, value: int = 1) -> None:
    """
    Fire-and-forget: increment a usage counter in Redis.

    This is a Celery task BUT it also works when called directly (without .delay())
    because the sync Redis client is always available.

    Called from API routes as a background task — never blocks the request.
    """
    _incr_redis_sync(project_id, metric, value)


def _incr_redis_sync(project_id: str, metric: str, value: int = 1) -> None:
    """
    Increment usage counter in Redis synchronously.
    Used by record_usage task AND by the inline increment helper below.
    """
    r = sync_redis.Redis(connection_pool=_redis_pool)
    try:
        r.incr(f"usage:{project_id}:{metric}", value)
    except Exception as exc:
        logger.warning("Failed to increment usage counter %s/%s: %s", project_id, metric, exc)
    finally:
        r.close()


async def increment_usage(project_id: str, metric: str, value: int = 1) -> None:
    """
    Async helper to increment a usage counter from async FastAPI routes.
    Runs the sync Redis INCR in a thread so the event loop is never blocked,
    and reuses the same sync connection pool as the Celery record_usage task —
    this avoids any SSL configuration mismatches from the async redis client.
    """
    import asyncio
    await asyncio.to_thread(_incr_redis_sync, project_id, metric, value)