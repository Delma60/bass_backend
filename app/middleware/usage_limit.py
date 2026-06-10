# backend/app/middleware/usage_limit.py
"""
Usage limit enforcement middleware.

Called as a FastAPI dependency on all /v1/* SDK routes.

Usage data is sourced in priority order:
  1. Pre-computed 30-day aggregates in Redis  (key: usage_agg:{project_id}:{metric})
     — written every 5 min by the Celery sync_all_project_usage task.
  2. Live Redis counters                      (key: usage:{project_id}:{metric})
     — incremented on every API request, flushed hourly.
  Both sources are summed so enforcement reflects usage within the current
  flush window, not just the last hourly snapshot.
  3. Direct DB aggregation                   (fallback when neither cache exists)
     — results cached under usage_agg:{project_id}:{metric} for USAGE_CACHE_TTL.

Plan limits are cached under plan_limits:{project_id} for PLAN_CACHE_TTL.
"""
import json
import logging
from typing import Any

from fastapi import HTTPException, Request

from app.db.redis import get_redis

logger = logging.getLogger(__name__)

# Cache TTLs
USAGE_CACHE_TTL = 300   # 5 min — matches Celery sync interval
PLAN_CACHE_TTL  = 600   # 10 min — plan changes are rare


def _get_metric_for_request(path: str, method: str) -> str | None:
    """Map an HTTP path + method to a usage metric."""
    if path.startswith("/v1/db"):
        return "db_writes" if method in ("POST", "PATCH", "DELETE") else "db_reads"
    if path.startswith("/v1/nosql"):
        return "nosql_writes" if method in ("POST", "PATCH", "DELETE", "PUT") else "nosql_reads"
    if path.startswith("/v1/storage"):
        return "storage_bytes"
    if path.startswith("/v1/functions"):
        return "function_calls"
    if path.startswith("/v1/ai"):
        return "ai_requests"
    return None


async def _fetch_plan_limits(project_id: str) -> dict[str, Any]:
    """
    Fetch plan limits for the project's org.
    Caches under plan_limits:{project_id} to avoid repeated DB hits.
    Returns a dict metric → limit (None = unlimited).
    """
    redis = await get_redis()
    cache_key = f"plan_limits:{project_id}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    from app.db.postgres import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text
        result = await session.execute(
            text("""
                SELECT pl.sql_rows, pl.nosql_docs, pl.storage_bytes,
                       pl.function_calls, pl.ai_requests
                FROM plan_limits pl
                JOIN organizations o ON o.plan = pl.plan
                JOIN projects p ON p.organization_id = o.id
                WHERE p.id = :project_id
            """),
            {"project_id": project_id},
        )
        row = result.mappings().first()

    if not row:
        # No plan row found — treat as unlimited to avoid blocking requests
        limits: dict[str, Any] = {}
    else:
        limits = {
            "db_reads":       row["sql_rows"],
            "db_writes":      row["sql_rows"],
            "nosql_reads":    row["nosql_docs"],
            "nosql_writes":   row["nosql_docs"],
            "storage_bytes":  row["storage_bytes"],
            "function_calls": row["function_calls"],
            "ai_requests":    row["ai_requests"],
        }

    await redis.setex(cache_key, PLAN_CACHE_TTL, json.dumps(limits))
    return limits


async def _fetch_usage(project_id: str, metric: str) -> int:
    """
    Return the current 30-day usage for a single metric.

    Sources (summed):
      A. Pre-computed aggregate:  usage_agg:{project_id}:{metric}
         Written by Celery sync_all_project_usage every 5 min.
      B. Live counter:            usage:{project_id}:{metric}
         Incremented per-request, flushed to Postgres hourly.

    If neither key exists, falls back to a direct DB aggregation and populates
    the usage_agg key so subsequent requests within the TTL window skip the DB.
    """
    redis = await get_redis()

    agg_key  = f"usage_agg:{project_id}:{metric}"
    live_key = f"usage:{project_id}:{metric}"

    # Fetch both keys in a single pipeline round-trip
    pipe = redis.pipeline()
    pipe.get(agg_key)
    pipe.get(live_key)
    agg_raw, live_raw = await pipe.execute()

    agg_val  = int(agg_raw)  if agg_raw  else None
    live_val = int(live_raw) if live_raw else 0

    if agg_val is not None:
        # Fast path: pre-computed aggregate exists
        return agg_val + live_val

    # Slow path: aggregate cache miss — query Postgres directly
    from app.db.postgres import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COALESCE(SUM(value), 0)::bigint AS total
                FROM usage_records
                WHERE project_id = :project_id
                  AND metric      = :metric
                  AND period_start >= NOW() - INTERVAL '30 days'
            """),
            {"project_id": project_id, "metric": metric},
        )
        db_total = int(result.scalar() or 0)

    # Populate the aggregate cache so the next request is fast
    await redis.setex(agg_key, USAGE_CACHE_TTL, str(db_total))

    return db_total + live_val


async def check_usage_limits(request: Request) -> None:
    """
    FastAPI dependency — enforces plan usage limits.
    Raises HTTP 429 with a structured payload if the project has exceeded its limit.
    Never blocks a request due to a monitoring failure (errors are logged + swallowed).
    """
    project_id = getattr(request.state, "project_id", None)
    if not project_id:
        return  # Not an authenticated SDK route

    metric = _get_metric_for_request(request.url.path, request.method)
    if not metric:
        return  # Route doesn't consume a metered resource

    try:
        limits = await _fetch_plan_limits(project_id)
        limit  = limits.get(metric)

        if limit is None:
            return  # Unlimited on this plan

        current = await _fetch_usage(project_id, metric)

        if current >= limit:
            if limit >= 1_000_000:
                limit_str = f"{limit // 1_000_000}M"
            elif limit >= 1_000:
                limit_str = f"{limit // 1_000}K"
            else:
                limit_str = str(limit)

            raise HTTPException(
                status_code=429,
                detail={
                    "code": "USAGE_LIMIT_EXCEEDED",
                    "message": (
                        f"You have reached the {metric.replace('_', ' ')} limit "
                        f"for your plan ({limit_str}). Upgrade to continue."
                    ),
                    "metric":  metric,
                    "limit":   limit,
                    "current": current,
                    "upgrade_url": "https://yourbaas.com/billing",
                },
            )
    except HTTPException:
        raise  # Re-raise 429s — don't swallow them
    except Exception as exc:
        # Never block a legitimate request because of a monitoring failure
        logger.warning(
            "Usage limit check failed for project %s metric %s: %s",
            project_id, metric, exc,
        )