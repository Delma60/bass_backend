# backend/app/api/internal/auth_browse.py
"""
Internal-only endpoints for the dashboard to browse and manage per-project auth users.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db, set_tenant_session

router = APIRouter(tags=["Internal Auth Browse"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


def _serialize_user(row: dict) -> dict:
    u = dict(row)
    for field in ("created_at", "updated_at"):
        if u.get(field) and hasattr(u[field], "isoformat"):
            u[field] = u[field].isoformat()
    return u


# ─── List users ────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/auth/users", dependencies=[InternalGuard])
async def list_auth_users(
    project_id: str,
    db_schema: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all users in the project's _auth_users table."""
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    await set_tenant_session(db, db_schema)

    where = "TRUE"
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if search:
        where = "(email ILIKE :search OR name ILIKE :search)"
        params["search"] = f"%{search}%"

    # Check which columns actually exist to be safe across schema versions
    try:
        col_result = await db.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = '_auth_users'
            """),
            {"schema": db_schema},
        )
        existing_cols = {r[0] for r in col_result}
    except Exception:
        existing_cols = {"id", "email", "name", "is_email_verified", "created_at"}

    # Build SELECT list based on what actually exists
    select_cols = ["id", "email", "name", "is_email_verified", "created_at"]
    if "updated_at" in existing_cols:
        select_cols.append("updated_at")

    select_clause = ", ".join(select_cols)

    try:
        result = await db.execute(
            text(f"""
                SELECT {select_clause}
                FROM "{db_schema}"."_auth_users"
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        users = [_serialize_user(dict(row._mapping)) for row in result]

        count_result = await db.execute(
            text(f'SELECT COUNT(*) FROM "{db_schema}"."_auth_users" WHERE {where}'),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )
        total = count_result.scalar() or 0

        return {"data": {"users": users, "total": total}}
    except Exception as e:
        logger.warning("Failed to list auth users for schema %s: %s", db_schema, e)
        return {"data": {"users": [], "total": 0}}


# ─── Get single user ───────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/auth/users/{user_id}", dependencies=[InternalGuard])
async def get_auth_user(
    project_id: str,
    user_id: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    await set_tenant_session(db, db_schema)

    result = await db.execute(
        text(f"""
            SELECT id, email, name, is_email_verified, created_at
            FROM "{db_schema}"."_auth_users"
            WHERE id = :user_id
        """),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return {"data": _serialize_user(dict(row))}


# ─── Create user ───────────────────────────────────────────────────────────────

class CreateAuthUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str | None = None


@router.post("/projects/{project_id}/auth/users", status_code=201, dependencies=[InternalGuard])
async def create_auth_user(
    project_id: str,
    body: CreateAuthUserRequest,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    from app.auth.project_auth import hash_password

    await set_tenant_session(db, db_schema)

    existing = await db.execute(
        text(f'SELECT id FROM "{db_schema}"."_auth_users" WHERE email = :email'),
        {"email": body.email},
    )
    if existing.first():
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = str(uuid.uuid4())
    hashed = hash_password(body.password)

    await db.execute(
        text(f"""
            INSERT INTO "{db_schema}"."_auth_users" (id, email, name, hashed_password, is_email_verified)
            VALUES (:id, :email, :name, :pwd, false)
        """),
        {"id": user_id, "email": body.email, "name": body.name or "", "pwd": hashed},
    )
    await db.commit()

    return {"data": {"id": user_id, "email": body.email, "name": body.name}}


# ─── Update user ───────────────────────────────────────────────────────────────

class UpdateAuthUserRequest(BaseModel):
    is_email_verified: bool | None = None
    name: str | None = None


@router.patch("/projects/{project_id}/auth/users/{user_id}", dependencies=[InternalGuard])
async def update_auth_user(
    project_id: str,
    user_id: str,
    body: UpdateAuthUserRequest,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    await set_tenant_session(db, db_schema)

    updates: dict[str, Any] = {}
    if body.is_email_verified is not None:
        updates["is_email_verified"] = body.is_email_verified
    if body.name is not None:
        updates["name"] = body.name

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["user_id"] = user_id

    result = await db.execute(
        text(f"""
            UPDATE "{db_schema}"."_auth_users"
            SET {set_clause}
            WHERE id = :user_id
            RETURNING id, email, name, is_email_verified
        """),
        updates,
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    return {"data": dict(row)}


# ─── Delete user ───────────────────────────────────────────────────────────────

@router.delete("/projects/{project_id}/auth/users/{user_id}", dependencies=[InternalGuard])
async def delete_auth_user(
    project_id: str,
    user_id: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    await set_tenant_session(db, db_schema)

    result = await db.execute(
        text(f'DELETE FROM "{db_schema}"."_auth_users" WHERE id = :user_id RETURNING id'),
        {"user_id": user_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    return {"data": {"deleted": True, "id": user_id}}


# ─── Reset password ────────────────────────────────────────────────────────────

class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8)


@router.post("/projects/{project_id}/auth/users/{user_id}/reset-password", dependencies=[InternalGuard])
async def reset_auth_user_password(
    project_id: str,
    user_id: str,
    body: ResetPasswordRequest,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    from app.auth.project_auth import hash_password

    hashed = hash_password(body.new_password)
    await set_tenant_session(db, db_schema)

    result = await db.execute(
        text(f"""
            UPDATE "{db_schema}"."_auth_users"
            SET hashed_password = :pwd
            WHERE id = :user_id
            RETURNING id
        """),
        {"pwd": hashed, "user_id": user_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    return {"data": {"id": user_id, "password_reset": True}}


# ─── Auth stats ────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/auth/stats", dependencies=[InternalGuard])
async def get_auth_stats(
    project_id: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not db_schema.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid schema name")

    await set_tenant_session(db, db_schema)

    try:
        result = await db.execute(
            text(f"""
                SELECT
                    COUNT(*)                                                    AS total_users,
                    COUNT(*) FILTER (WHERE is_email_verified = true)            AS verified_users,
                    COUNT(*) FILTER (WHERE is_email_verified = false)           AS unverified_users,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS new_last_30d,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS new_last_7d
                FROM "{db_schema}"."_auth_users"
            """),
        )
        row = result.mappings().first()
        return {"data": dict(row) if row else {}}
    except Exception:
        return {"data": {
            "total_users": 0,
            "verified_users": 0,
            "unverified_users": 0,
            "new_last_30d": 0,
            "new_last_7d": 0,
        }}