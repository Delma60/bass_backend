# backend/app/api/admin_api/storage.py
"""
/admin-api/storage — storage statistics for the admin panel.

The admin panel does NOT get direct access to files or presigned URLs —
only aggregate stats (file count, total size) per project/bucket.
Actual file management stays in the developer dashboard.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_api.middleware import AdminContext, require_admin_panel
from app.db.postgres import get_db
from app.engines.storage_engine import get_bucket_stats, list_files

router = APIRouter(prefix="/storage", tags=["Admin API — Storage"])
logger = logging.getLogger(__name__)


@router.get("/stats")
async def platform_storage_stats(
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Platform-wide storage summary.
    Returns per-project storage usage from the usage_records table.
    """
    result = await db.execute(
        text("""
            SELECT project_id, SUM(value) AS storage_bytes
            FROM usage_records
            WHERE metric = 'storage_bytes'
              AND period_start >= NOW() - INTERVAL '30 days'
            GROUP BY project_id
            ORDER BY storage_bytes DESC
            LIMIT 100
        """),
    )
    rows = [
        {"project_id": r["project_id"], "storage_bytes": int(r["storage_bytes"])}
        for r in result.mappings()
    ]
    total_bytes = sum(r["storage_bytes"] for r in rows)

    return {
        "data": {
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / (1024**3), 3),
            "projects": rows,
        }
    }


@router.get("/{project_id}/stats")
async def project_storage_stats(
    project_id: str,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate storage stats for a single project across all its buckets."""
    import asyncio
    from app.storage.minio import get_s3_client

    s3 = get_s3_client()

    def _list_project_buckets() -> list[str]:
        try:
            response = s3.list_buckets()
            safe_project = project_id.lower().replace("_", "-")
            prefix = f"{safe_project}-"
            return [
                b["Name"].removeprefix(prefix)
                for b in response.get("Buckets", [])
                if b["Name"].startswith(prefix)
            ]
        except Exception as exc:
            logger.warning("Could not list buckets for project %s: %s", project_id, exc)
            return []

    bucket_names = await asyncio.to_thread(_list_project_buckets)

    bucket_stats = []
    total_files = 0
    total_bytes = 0

    for bucket in bucket_names:
        try:
            stats = await get_bucket_stats(project_id, bucket)
            bucket_stats.append({"bucket": bucket, **stats})
            total_files += stats.get("file_count", 0)
            total_bytes += stats.get("total_size", 0)
        except Exception as exc:
            logger.warning("Could not get stats for bucket %s/%s: %s", project_id, bucket, exc)
            bucket_stats.append({"bucket": bucket, "file_count": 0, "total_size": 0})

    return {
        "data": {
            "project_id": project_id,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / (1024**3), 3),
            "buckets": bucket_stats,
        }
    }