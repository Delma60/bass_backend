# backend/app/api/superadmin/organizations.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.requests import OrgPlanOverrideRequest
from app.models.staff import StaffContext
from app.api.superadmin._audit import write_audit_log

router = APIRouter(prefix="/organizations")
logger = logging.getLogger(__name__)


@router.get("")
async def list_organizations(
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    where = "TRUE"
    if search:
        where = "o.name ILIKE :search"
        params["search"] = f"%{search}%"

    result = await db.execute(
        text(f"""
            SELECT o.id, o.name, o.plan, o.created_at,
                   COUNT(p.id) AS project_count,
                   u.email AS owner_email
            FROM organizations o
            LEFT JOIN projects p ON p.organization_id = o.id
            LEFT JOIN users u ON u.id = o.owner_id
            WHERE {where}
            GROUP BY o.id, u.email
            ORDER BY o.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    orgs = [dict(r) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM organizations o WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": orgs, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{org_id}")
async def get_organization(
    org_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT o.id, o.name, o.plan, o.created_at,
                   u.email AS owner_email, u.id AS owner_id,
                   json_agg(json_build_object(
                       'id', p.id, 'name', p.name, 'status', p.status
                   )) FILTER (WHERE p.id IS NOT NULL) AS projects
            FROM organizations o
            LEFT JOIN users u ON u.id = o.owner_id
            LEFT JOIN projects p ON p.organization_id = o.id
            WHERE o.id = :org_id
            GROUP BY o.id, u.email, u.id
        """),
        {"org_id": org_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Organization not found")
    return {"data": dict(row)}


@router.patch("/{org_id}/plan")
async def override_org_plan(
    org_id: str,
    body: OrgPlanOverrideRequest,
    staff: StaffContext = Depends(require_staff_role(StaffRole.billing)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    valid_plans = {"free", "starter", "pro"}
    if body.plan not in valid_plans:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Must be one of: {valid_plans}")

    result = await db.execute(
        text("UPDATE organizations SET plan = :plan WHERE id = :org_id RETURNING id, name, plan"),
        {"plan": body.plan, "org_id": org_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Organization not found")
    await db.commit()

    await write_audit_log(db, staff, "org.plan_override", org_id, meta={"plan": body.plan, "reason": body.reason})
    return {"data": dict(row)}


@router.delete("/{org_id}")
async def delete_organization(
    org_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("DELETE FROM organizations WHERE id = :org_id RETURNING id"),
        {"org_id": org_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Organization not found")
    await db.commit()

    await write_audit_log(db, staff, "org.delete", org_id)
    return {"data": {"deleted": True, "id": org_id}}