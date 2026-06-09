# backend/app/api/superadmin/flags.py
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.requests import FeatureFlagUpdateRequest
from app.models.staff import StaffContext
from app.api.superadmin._audit import write_audit_log

router = APIRouter(prefix="/flags")
logger = logging.getLogger(__name__)


@router.get("")
async def list_flags(
    staff: StaffContext = Depends(require_staff_role(StaffRole.ops)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("SELECT id, name, enabled, description, updated_at FROM feature_flags ORDER BY name")
    )
    flags = [dict(r) for r in result.mappings()]
    return {"data": flags, "meta": {"count": len(flags)}}


@router.get("/{flag_name}")
async def get_flag(
    flag_name: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.ops)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("SELECT id, name, enabled, description, updated_at FROM feature_flags WHERE name = :name"),
        {"name": flag_name},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    return {"data": dict(row)}


@router.put("/{flag_name}")
async def upsert_flag(
    flag_name: str,
    body: FeatureFlagUpdateRequest,
    staff: StaffContext = Depends(require_staff_role(StaffRole.ops)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create or update a feature flag."""
    await db.execute(
        text("""
            INSERT INTO feature_flags (id, name, enabled, description, updated_at)
            VALUES (:id, :name, :enabled, :description, NOW())
            ON CONFLICT (name) DO UPDATE
              SET enabled = EXCLUDED.enabled,
                  description = COALESCE(EXCLUDED.description, feature_flags.description),
                  updated_at = NOW()
        """),
        {
            "id": str(uuid.uuid4()),
            "name": flag_name,
            "enabled": body.enabled,
            "description": body.description,
        },
    )
    await db.commit()

    # Bust cache
    from app.db.redis import get_redis
    redis = await get_redis()
    await redis.delete(f"flag:{flag_name}")

    await write_audit_log(db, staff, "flag.update", flag_name, meta={"enabled": body.enabled})

    result = await db.execute(
        text("SELECT id, name, enabled, description, updated_at FROM feature_flags WHERE name = :name"),
        {"name": flag_name},
    )
    row = result.mappings().first()
    return {"data": dict(row) if row else {"name": flag_name, "enabled": body.enabled}}