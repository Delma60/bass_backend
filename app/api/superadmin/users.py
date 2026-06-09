# backend/app/api/superadmin/users.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.requests import UserUpdateRequest
from app.models.staff import StaffContext
from app.api.superadmin._audit import write_audit_log

router = APIRouter(prefix="/users", dependencies=[Depends(require_staff_role(StaffRole.support))])
logger = logging.getLogger(__name__)


@router.get("")
async def list_users(
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    where = "TRUE"
    if search:
        where = "(u.email ILIKE :search OR u.name ILIKE :search)"
        params["search"] = f"%{search}%"

    result = await db.execute(
        text(f"""
            SELECT u.id, u.email, u.name, u.is_email_verified,
                   u.is_banned, u.created_at,
                   COUNT(DISTINCT o.id) AS org_count
            FROM users u
            LEFT JOIN organization_members om ON om.user_id = u.id
            LEFT JOIN organizations o ON o.id = om.organization_id
            WHERE {where}
            GROUP BY u.id
            ORDER BY u.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    users = [dict(r) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM users u WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": users, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{user_id}")
async def get_user(
    user_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT u.id, u.email, u.name, u.is_email_verified,
                   u.is_banned, u.created_at,
                   json_agg(
                       json_build_object(
                           'id', o.id, 'name', o.name, 'plan', o.plan,
                           'role', om.role
                       )
                   ) FILTER (WHERE o.id IS NOT NULL) AS organizations
            FROM users u
            LEFT JOIN organization_members om ON om.user_id = u.id
            LEFT JOIN organizations o ON o.id = om.organization_id
            WHERE u.id = :user_id
            GROUP BY u.id
        """),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"data": dict(row)}


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
        text(f"UPDATE users SET {set_clause} WHERE id = :user_id RETURNING id, email, is_banned, is_email_verified"),
        updates,
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    action = "user.ban" if body.is_banned else "user.update"
    await write_audit_log(db, staff, action, user_id, meta=dict(updates))
    return {"data": dict(row)}


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("DELETE FROM users WHERE id = :user_id RETURNING id"),
        {"user_id": user_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    await write_audit_log(db, staff, "user.delete", user_id)
    return {"data": {"deleted": True, "id": user_id}}