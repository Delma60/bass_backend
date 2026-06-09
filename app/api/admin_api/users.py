# backend/app/api/admin_api/users.py
"""
/admin-api/users — developer account management for the admin panel.

Mirrors the superadmin users endpoints but uses AdminContext auth instead
of StaffContext. The admin panel may read all users, update ban/verify status,
and permanently delete accounts.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_api._audit import write_admin_audit
from app.api.admin_api.middleware import AdminContext, require_admin_panel
from app.db.postgres import get_db

router = APIRouter(prefix="/users", tags=["Admin API — Users"])
logger = logging.getLogger(__name__)


def _serialize(row: dict) -> dict:
    r = dict(row)
    for field in ("created_at", "updated_at", "last_login_at"):
        if r.get(field) and hasattr(r[field], "isoformat"):
            r[field] = r[field].isoformat()
    return r


@router.get("")
async def list_users(
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    """List all developer accounts with optional email/name search."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    where = "TRUE"
    if search:
        where = "(u.email ILIKE :search OR u.name ILIKE :search)"
        params["search"] = f"%{search}%"

    result = await db.execute(
        text(f"""
            SELECT u.id, u.email, u.name, u.is_email_verified,
                   u.is_banned, u.created_at,
                   COUNT(DISTINCT p.id) AS project_count
            FROM users u
            LEFT JOIN organizations o ON o.owner_id = u.id
            LEFT JOIN projects p ON p.organization_id = o.id
            WHERE {where}
            GROUP BY u.id
            ORDER BY u.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    users = [_serialize(dict(r)) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM users u WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": users, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{user_id}")
async def get_user(
    user_id: str,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch a single developer account with their organizations and projects."""
    result = await db.execute(
        text("""
            SELECT u.id, u.email, u.name, u.is_email_verified,
                   u.is_banned, u.created_at,
                   json_agg(
                       json_build_object(
                           'id', o.id, 'name', o.name, 'plan', o.plan,
                           'project_count', (
                               SELECT COUNT(*) FROM projects p
                               WHERE p.organization_id = o.id
                           )
                       )
                   ) FILTER (WHERE o.id IS NOT NULL) AS organizations
            FROM users u
            LEFT JOIN organizations o ON o.owner_id = u.id
            WHERE u.id = :user_id
            GROUP BY u.id
        """),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"data": _serialize(dict(row))}


class UpdateUserRequest(BaseModel):
    is_banned: bool | None = None
    is_email_verified: bool | None = None
    reason: str | None = None


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update a developer account — ban, unban, or verify email."""
    updates: dict[str, Any] = {}
    if body.is_banned is not None:
        updates["is_banned"] = body.is_banned
    if body.is_email_verified is not None:
        updates["is_email_verified"] = body.is_email_verified

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["user_id"] = user_id

    result = await db.execute(
        text(
            f"UPDATE users SET {set_clause} WHERE id = :user_id "
            f"RETURNING id, email, is_banned, is_email_verified"
        ),
        updates,
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    action = "admin_panel.user.ban" if body.is_banned else "admin_panel.user.update"
    await write_admin_audit(
        db, ctx, action, user_id,
        meta={"reason": body.reason, **{k: v for k, v in updates.items() if k != "user_id"}},
    )
    return {"data": dict(row)}


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permanently delete a developer account and all associated data."""
    result = await db.execute(
        text("DELETE FROM users WHERE id = :user_id RETURNING id, email"),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    await write_admin_audit(
        db, ctx, "admin_panel.user.delete", user_id,
        meta={"email": row["email"]},
    )
    return {"data": {"deleted": True, "id": user_id}}