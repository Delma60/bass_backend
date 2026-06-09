# backend/app/api/superadmin/staff.py
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.staff_auth import hash_staff_password, issue_staff_token
from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.requests import StaffInviteRequest, StaffUpdateRoleRequest
from app.models.staff import StaffContext
from app.api.superadmin._audit import write_audit_log

router = APIRouter(prefix="/staff")
logger = logging.getLogger(__name__)

VALID_ROLES = {"super_admin", "ops", "billing", "support"}


@router.get("")
async def list_staff(
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT id, email, name, role, is_active, last_login_at, created_at
            FROM staff
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": limit, "offset": offset},
    )
    members = [dict(r) for r in result.mappings()]

    count_result = await db.execute(text("SELECT COUNT(*) FROM staff"))
    total = count_result.scalar() or 0

    return {"data": members, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.post("", status_code=201)
async def invite_staff(
    body: StaffInviteRequest,
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    # Check email uniqueness
    existing = await db.execute(
        text("SELECT id FROM staff WHERE email = :email"),
        {"email": body.email},
    )
    if existing.first():
        raise HTTPException(status_code=409, detail="Staff member with this email already exists")

    member_id = str(uuid.uuid4())
    # Generate a temporary password that must be changed on first login
    import secrets
    temp_password = secrets.token_urlsafe(16)
    hashed = hash_staff_password(temp_password)

    await db.execute(
        text("""
            INSERT INTO staff (id, email, name, hashed_password, role, is_active, invited_by)
            VALUES (:id, :email, :name, :pwd, :role, true, :invited_by)
        """),
        {
            "id": member_id,
            "email": body.email,
            "name": body.name,
            "pwd": hashed,
            "role": body.role,
            "invited_by": staff.id,
        },
    )
    await db.commit()

    await write_audit_log(db, staff, "staff.invite", member_id, meta={"email": body.email, "role": body.role})

    return {
        "data": {
            "id": member_id,
            "email": body.email,
            "name": body.name,
            "role": body.role,
            "temp_password": temp_password,  # Caller sends this via email
        }
    }


@router.patch("/{member_id}/role")
async def update_staff_role(
    member_id: str,
    body: StaffUpdateRoleRequest,
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    if member_id == staff.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    result = await db.execute(
        text("UPDATE staff SET role = :role WHERE id = :id RETURNING id, email, name, role"),
        {"role": body.role, "id": member_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Staff member not found")
    await db.commit()

    await write_audit_log(db, staff, "staff.update_role", member_id, meta={"new_role": body.role})
    return {"data": dict(row)}


@router.delete("/{member_id}")
async def deactivate_staff(
    member_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if member_id == staff.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")

    result = await db.execute(
        text("UPDATE staff SET is_active = false WHERE id = :id RETURNING id"),
        {"id": member_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Staff member not found")
    await db.commit()

    await write_audit_log(db, staff, "staff.deactivate", member_id)
    return {"data": {"id": member_id, "deactivated": True}}


@router.post("/login")
async def staff_login(
    db: AsyncSession = Depends(get_db),
    # No staff auth guard — this IS the login endpoint
    body: Any = None,
) -> dict[str, Any]:
    """Staff login — issues a staff JWT. Called before X-Staff-Token exists."""
    from pydantic import BaseModel
    from fastapi import Body
    raise HTTPException(status_code=405, detail="Use POST /superadmin/staff/login with email+password body")


from pydantic import BaseModel


class StaffLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
async def staff_auth_login(
    body: StaffLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Staff authentication — no token required.
    Returns a staff JWT for use as X-Staff-Token on subsequent requests.
    """
    from app.auth.staff_auth import verify_staff_password

    result = await db.execute(
        text("SELECT id, email, name, role, hashed_password, is_active FROM staff WHERE email = :email"),
        {"email": body.email},
    )
    row = result.mappings().first()

    if not row or not verify_staff_password(body.password, row["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    # Update last login
    await db.execute(
        text("UPDATE staff SET last_login_at = NOW() WHERE id = :id"),
        {"id": row["id"]},
    )
    await db.commit()

    token = issue_staff_token(row["id"], row["email"], row["name"], row["role"])

    return {
        "data": {
            "staff": {
                "id": row["id"],
                "email": row["email"],
                "name": row["name"],
                "role": row["role"],
            },
            "token": token,
            "token_type": "bearer",
        }
    }