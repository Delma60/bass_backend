# backend/app/api/admin_api/audit.py
"""
/admin-api/audit — read-only audit log access for the admin panel.

Exposes the same append-only audit_logs table used by the superadmin panel.
The admin panel may filter by action prefix, actor, resource, or date range.
Audit logs can never be deleted through any API — append-only by design.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_api.middleware import AdminContext, require_admin_panel
from app.db.postgres import get_db

router = APIRouter(prefix="/audit", tags=["Admin API — Audit"])
logger = logging.getLogger(__name__)


@router.get("")
async def list_audit_logs(
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(
        default=None,
        description="Filter by action prefix, e.g. 'admin_panel.' or 'user.'",
    ),
    actor_id: str | None = Query(default=None),
    resource: str | None = Query(default=None),
    since: str | None = Query(
        default=None,
        description="ISO 8601 timestamp — return entries after this point",
    ),
) -> dict[str, Any]:
    """
    Platform-wide audit log. Read-only.
    Returns most recent entries first, filtered by optional criteria.
    """
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if action:
        conditions.append("al.action LIKE :action")
        params["action"] = f"{action}%"
    if actor_id:
        conditions.append("al.actor_id = :actor_id")
        params["actor_id"] = actor_id
    if resource:
        conditions.append("al.resource = :resource")
        params["resource"] = resource
    if since:
        conditions.append("al.created_at > :since::timestamptz")
        params["since"] = since

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT al.id, al.actor_id, al.actor_role, al.action,
                   al.resource, al.meta, al.created_at
            FROM audit_logs al
            WHERE {where}
            ORDER BY al.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )

    logs = []
    for r in result.mappings():
        entry = dict(r)
        if entry.get("created_at") and hasattr(entry["created_at"], "isoformat"):
            entry["created_at"] = entry["created_at"].isoformat()
        logs.append(entry)

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM audit_logs al WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": logs, "meta": {"count": total, "limit": limit, "offset": offset}}