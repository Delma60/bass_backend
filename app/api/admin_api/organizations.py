# backend/app/api/admin_api/organizations.py
"""
/admin-api/organizations — organization management for the admin panel.
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

router = APIRouter(prefix="/organizations", tags=["Admin API — Organizations"])
logger = logging.getLogger(__name__)

VALID_PLANS = {"free", "starter", "pro"}


def _serialize(row: dict) -> dict:
    r = dict(row)
    for field in ("created_at",):
        if r.get(field) and hasattr(r[field], "isoformat"):
            r[field] = r[field].isoformat()
    return r


@router.get("")
async def list_organizations(
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    plan: str | None = Query(default=None),
) -> dict[str, Any]:
    """List all organizations with optional name search and plan filter."""
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if search:
        conditions.append("o.name ILIKE :search")
        params["search"] = f"%{search}%"
    if plan:
        conditions.append("o.plan = :plan")
        params["plan"] = plan

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT o.id, o.name, o.plan, o.created_at,
                   COUNT(p.id) AS project_count,
                   u.email AS owner_email, u.id AS owner_id
            FROM organizations o
            LEFT JOIN projects p ON p.organization_id = o.id
            LEFT JOIN users u ON u.id = o.owner_id
            WHERE {where}
            GROUP BY o.id, u.email, u.id
            ORDER BY o.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    orgs = [_serialize(dict(r)) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM organizations o WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": orgs, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{org_id}")
async def get_organization(
    org_id: str,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get a single organization with its projects and owner info."""
    result = await db.execute(
        text("""
            SELECT o.id, o.name, o.plan, o.created_at,
                   u.email AS owner_email, u.id AS owner_id,
                   json_agg(
                       json_build_object(
                           'id', p.id, 'name', p.name,
                           'status', p.status, 'region', p.region
                       )
                   ) FILTER (WHERE p.id IS NOT NULL) AS projects
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
    return {"data": _serialize(dict(row))}


class PlanOverrideRequest(BaseModel):
    plan: str
    reason: str | None = None


@router.patch("/{org_id}/plan")
async def override_plan(
    org_id: str,
    body: PlanOverrideRequest,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Override the billing plan for an organization."""
    if body.plan not in VALID_PLANS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid plan. Must be one of: {VALID_PLANS}",
        )

    result = await db.execute(
        text(
            "UPDATE organizations SET plan = :plan WHERE id = :org_id "
            "RETURNING id, name, plan"
        ),
        {"plan": body.plan, "org_id": org_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Organization not found")
    await db.commit()

    await write_admin_audit(
        db, ctx, "admin_panel.org.plan_override", org_id,
        meta={"plan": body.plan, "reason": body.reason},
    )
    return {"data": dict(row)}


@router.delete("/{org_id}")
async def delete_organization(
    org_id: str,
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Permanently delete an organization and cascade to its projects."""
    result = await db.execute(
        text("DELETE FROM organizations WHERE id = :org_id RETURNING id, name"),
        {"org_id": org_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Organization not found")
    await db.commit()

    await write_admin_audit(
        db, ctx, "admin_panel.org.delete", org_id,
        meta={"name": row["name"]},
    )
    return {"data": {"deleted": True, "id": org_id}}