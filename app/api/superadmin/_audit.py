# backend/app/api/superadmin/_audit.py
import json
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.staff import StaffContext

logger = logging.getLogger(__name__)


async def write_audit_log(
    db: AsyncSession,
    staff: StaffContext,
    action: str,
    resource: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append an audit log entry. Audit logs are never deleted."""
    try:
        await db.execute(
            text("""
                INSERT INTO audit_logs (id, actor_id, actor_role, action, resource, meta, created_at)
                VALUES (:id, :actor_id, :actor_role, :action, :resource, :meta, NOW())
            """),
            {
                "id": str(uuid.uuid4()),
                "actor_id": staff.id,
                "actor_role": staff.role.value,
                "action": action,
                "resource": resource,
                "meta": json.dumps(meta) if meta else None,
            },
        )
        await db.commit()
    except Exception as e:
        logger.error("Failed to write audit log for %s.%s: %s", action, resource, e)