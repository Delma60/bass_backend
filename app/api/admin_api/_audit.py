# backend/app/api/admin_api/_audit.py
"""
Audit log writer for /admin-api/* routes.

Every mutating action (PATCH, DELETE) from the admin panel is written here.
Reads are NOT logged by default — pass log_reads=True on sensitive read routes.

Audit entries are stored in the same `audit_logs` table used by the superadmin
panel so the platform has a unified, append-only audit trail.
The actor_role is recorded as 'admin_panel' to distinguish from BaaS staff actions.
"""
import json
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_api.middleware import AdminContext

logger = logging.getLogger(__name__)


async def write_admin_audit(
    db: AsyncSession,
    ctx: AdminContext,
    action: str,
    resource: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """
    Append an audit log entry originating from the admin panel.
    Never raises — a failed audit write is logged but does not roll back the
    main operation.
    """
    try:
        await db.execute(
            text("""
                INSERT INTO audit_logs
                    (id, actor_id, actor_role, action, resource, meta, created_at)
                VALUES
                    (:id, :actor_id, :actor_role, :action, :resource, :meta, NOW())
            """),
            {
                "id": str(uuid.uuid4()),
                "actor_id": ctx.key_id,
                "actor_role": "admin_panel",
                "action": action,
                "resource": resource,
                "meta": json.dumps(
                    {"project_id": ctx.project_id, "label": ctx.label, **(meta or {})}
                ),
            },
        )
        await db.commit()
    except Exception as exc:
        logger.error(
            "Failed to write admin audit log [action=%s resource=%s]: %s",
            action,
            resource,
            exc,
        )