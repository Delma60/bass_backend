# backend/app/tasks/usage_sync.py
"""
Usage sync tasks.

The API layer increments lightweight Redis counters on each request:
  INCR usage:{project_id}:db_reads
  INCR usage:{project_id}:db_writes
  INCR usage:{project_id}:storage_bytes
  INCR usage:{project_id}:function_calls
  INCR usage:{project_id}:nosql_reads
  INCR usage:{project_id}:nosql_writes

The flush_redis_counters task (hourly) reads and resets these counters,
writing aggregated rows into the usage_records Postgres table.

sync_all_project_usage (every 5 min) re-aggregates from usage_records
for the billing dashboard.
"""
import asyncio
import logging
from datetime import datetime, timezone

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
    from app.config import settings
    from app.db.postgres import AsyncSessionLocal

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    flushed: dict[str, int] = {}

    try:
        # Scan all usage keys
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
            # key format: usage:{project_id}:{metric}
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
    from app.config import settings
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
    Called from API routes as a background task — never blocks the request.
    """
    import redis as sync_redis
    from app.config import settings

    r = sync_redis.from_url(settings.redis_url, decode_responses=True)
    try:
        r.incr(f"usage:{project_id}:{metric}", value)
    finally:
        r.close()