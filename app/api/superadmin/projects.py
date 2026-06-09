# backend/app/api/superadmin/projects.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.requests import ProjectStatusRequest
from app.models.staff import StaffContext
from app.api.superadmin._audit import write_audit_log

router = APIRouter(prefix="/projects")
logger = logging.getLogger(__name__)


@router.get("")
async def list_projects(
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if status:
        conditions.append("p.status = :status")
        params["status"] = status
    if search:
        conditions.append("p.name ILIKE :search")
        params["search"] = f"%{search}%"

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT p.id, p.name, p.status, p.region, p.created_at,
                   o.name AS org_name, o.plan AS org_plan
            FROM projects p
            LEFT JOIN organizations o ON o.id = p.organization_id
            WHERE {where}
            ORDER BY p.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    projects = [dict(r) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM projects p WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": projects, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.support)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT p.id, p.name, p.status, p.region, p.db_schema,
                   p.mongo_database, p.created_at,
                   o.id AS org_id, o.name AS org_name, o.plan AS org_plan
            FROM projects p
            LEFT JOIN organizations o ON o.id = p.organization_id
            WHERE p.id = :project_id
        """),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"data": dict(row)}


@router.patch("/{project_id}/status")
async def update_project_status(
    project_id: str,
    body: ProjectStatusRequest,
    staff: StaffContext = Depends(require_staff_role(StaffRole.ops)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    valid_statuses = {"active", "paused"}
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {valid_statuses}")

    result = await db.execute(
        text("UPDATE projects SET status = :status WHERE id = :project_id RETURNING id, name, status"),
        {"status": body.status, "project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.commit()

    # Bust the API key cache entries for this project so status takes effect immediately
    from app.db.redis import get_redis
    redis = await get_redis()
    async for key in redis.scan_iter(f"apikey:*"):
        cached = await redis.get(key)
        if cached:
            import json
            data = json.loads(cached)
            if data.get("project_id") == project_id:
                await redis.delete(key)

    await write_audit_log(db, staff, f"project.{body.status}", project_id)
    return {"data": dict(row)}


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.super_admin)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Get schema/db info before deleting
    result = await db.execute(
        text("SELECT db_schema, mongo_database FROM projects WHERE id = :project_id"),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    # Teardown infrastructure
    from app.provisioner.sql_provisioner import teardown_project_schema
    from app.provisioner.nosql_provisioner import teardown_project_database
    await teardown_project_schema(row["db_schema"])
    await teardown_project_database(row["mongo_database"])

    await db.execute(
        text("DELETE FROM projects WHERE id = :project_id"),
        {"project_id": project_id},
    )
    await db.commit()

    await write_audit_log(db, staff, "project.delete", project_id)
    return {"data": {"deleted": True, "id": project_id}}