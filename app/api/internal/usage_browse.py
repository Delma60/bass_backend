# backend/app/api/internal/usage_browse.py
"""
Internal usage endpoint — returns current 30-day rolling usage for a project.

Usage is assembled from two sources and summed so the dashboard always shows
near-real-time numbers:

  A. Postgres usage_records  — hourly flushes from Celery flush_redis_counters
  B. Live Redis counters     — usage:{project_id}:{metric}, incremented per-request
     (works without Celery — incremented directly via increment_usage())

The response also includes plan limits from plan_limits so the frontend can
render progress bars without hardcoding any numbers.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(tags=["Internal Usage"])
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


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


async def _get_live_redis_counters(project_id: str) -> dict[str, int]:
    """
    Read the live (not-yet-flushed) Redis counters for a project.
    These are incremented directly by increment_usage() on every CRUD operation,
    so they always reflect the current state even without Celery running.
    """
    try:
        from app.db.redis import get_redis
        redis = await get_redis()
        pipe = redis.pipeline()
        for metric in TRACKED_METRICS:
            pipe.get(f"usage:{project_id}:{metric}")
        values = await pipe.execute()
        result = {}
        for metric, v in zip(TRACKED_METRICS, values):
            result[metric] = int(v) if v else 0
        return result
    except Exception as exc:
        logger.warning("Could not read live Redis counters for %s: %s", project_id, exc)
        return {m: 0 for m in TRACKED_METRICS}


async def _get_postgres_usage(project_id: str, db: AsyncSession) -> dict[str, int]:
    """
    Read the 30-day aggregated usage from Postgres usage_records.
    These are written by the hourly Celery flush task.
    Note: after a flush, the live Redis counters are reset to 0, so the
    Postgres values represent the bulk of historical usage.
    """
    try:
        pg_result = await db.execute(
            text("""
                SELECT metric, SUM(value)::bigint AS total
                FROM usage_records
                WHERE project_id  = :project_id
                  AND period_start >= NOW() - INTERVAL '30 days'
                GROUP BY metric
            """),
            {"project_id": project_id},
        )
        return {r["metric"]: int(r["total"]) for r in pg_result.mappings()}
    except Exception as exc:
        logger.warning("Could not read Postgres usage for %s: %s", project_id, exc)
        return {}


@router.get("/usage/{project_id}", dependencies=[InternalGuard])
async def get_project_usage(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    30-day rolling usage + plan limits + derived display stats.

    Usage = Postgres aggregation (flushed hourly) + live Redis counters (current hour).
    Both are always fetched and summed — this ensures the dashboard reflects
    operations even when Celery workers are not running.
    """
    # Fetch both sources in parallel
    import asyncio
    pg_totals, live_totals = await asyncio.gather(
        _get_postgres_usage(project_id, db),
        _get_live_redis_counters(project_id),
    )

    # Merge: sum Postgres flushed + live Redis
    merged: dict[str, int] = {}
    for metric in TRACKED_METRICS:
        merged[metric] = pg_totals.get(metric, 0) + live_totals.get(metric, 0)

    # Plan limits
    limits_result = await db.execute(
        text("""
            SELECT pl.sql_rows, pl.nosql_docs, pl.storage_bytes,
                   pl.function_calls, pl.ai_requests,
                   pl.api_calls_per_min, pl.team_members,
                   pl.price_ngn, pl.price_usd, pl.plan
            FROM plan_limits pl
            JOIN organizations o ON o.plan = pl.plan
            JOIN projects p ON p.organization_id = o.id
            WHERE p.id = :project_id
        """),
        {"project_id": project_id},
    )
    limits_row = limits_result.mappings().first()

    plan_limits: dict[str, Any] = {}
    if limits_row:
        plan_limits = {
            "db_reads":          limits_row["sql_rows"],
            "db_writes":         limits_row["sql_rows"],
            "nosql_reads":       limits_row["nosql_docs"],
            "nosql_writes":      limits_row["nosql_docs"],
            "storage_bytes":     limits_row["storage_bytes"],
            "function_calls":    limits_row["function_calls"],
            "ai_requests":       limits_row["ai_requests"],
            "api_calls_per_min": limits_row["api_calls_per_min"],
            "team_members":      limits_row["team_members"],
            "price_ngn":         float(limits_row["price_ngn"]),
            "price_usd":         float(limits_row["price_usd"]),
            "plan":              limits_row["plan"],
        }

    # Auth user count (from tenant schema)
    auth_users = 0
    db_schema: str | None = None
    try:
        schema_result = await db.execute(
            text("SELECT db_schema FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        schema_row = schema_result.mappings().first()
        if schema_row:
            db_schema = schema_row["db_schema"]
            count_result = await db.execute(
                text(f'SELECT COUNT(*) FROM "{db_schema}"."_auth_users"'),
            )
            auth_users = int(count_result.scalar() or 0)
    except Exception as exc:
        logger.debug("Could not count auth users for %s: %s", project_id, exc)

    # SQL row count (pg_stat fast estimate)
    sql_rows = 0
    if db_schema:
        try:
            sql_result = await db.execute(
                text("""
                    SELECT COALESCE(SUM(n_live_tup), 0)::bigint AS total_rows
                    FROM pg_stat_user_tables
                    WHERE schemaname = :schema
                """),
                {"schema": db_schema},
            )
            sql_rows = int(sql_result.scalar() or 0)
        except Exception as exc:
            logger.debug("Could not count SQL rows for %s: %s", project_id, exc)

    storage_bytes = merged.get("storage_bytes", 0)

    # Log what we found for debugging
    logger.debug(
        "Usage for %s — PG: %s | Live: %s | Merged: %s",
        project_id, pg_totals, live_totals, merged,
    )

    return {
        "data": {
            # Per-metric usage (Postgres flushed + live Redis combined)
            "db_reads":       merged.get("db_reads", 0),
            "db_writes":      merged.get("db_writes", 0),
            "nosql_reads":    merged.get("nosql_reads", 0),
            "nosql_writes":   merged.get("nosql_writes", 0),
            "storage_bytes":  storage_bytes,
            "function_calls": merged.get("function_calls", 0),
            "ai_requests":    merged.get("ai_requests", 0),
            # Derived display aliases (used by overview page)
            "apiCalls": (
                merged.get("db_reads", 0)    + merged.get("db_writes", 0) +
                merged.get("nosql_reads", 0) + merged.get("nosql_writes", 0)
            ),
            "authUsers":     auth_users,
            "sqlRows":       sql_rows,
            "storageUsedMb": round(storage_bytes / (1024 * 1024), 2),
            # Plan limits from DB — never hardcoded
            "limits": plan_limits,
        }
    }