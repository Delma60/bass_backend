# backend/app/api/superadmin/audit.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.staff import StaffContext

router = APIRouter(prefix="/audit")
logger = logging.getLogger(__name__)


@router.get("")
async def list_audit_logs(
    staff: StaffContext = Depends(require_staff_role(StaffRole.ops)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, description="Filter by action prefix, e.g. 'user.'"),
    actor_id: str | None = Query(default=None),
    resource: str | None = Query(default=None),
) -> dict[str, Any]:
    """Platform-wide audit log. Append-only — no mutations allowed."""
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

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT al.id, al.actor_id, al.actor_role, al.action,
                   al.resource, al.meta, al.created_at,
                   s.email AS actor_email, s.name AS actor_name
            FROM audit_logs al
            LEFT JOIN staff s ON s.id = al.actor_id
            WHERE {where}
            ORDER BY al.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    logs = [dict(r) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM audit_logs al WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": logs, "meta": {"count": total, "limit": limit, "offset": offset}}