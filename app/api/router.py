import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db, set_tenant_session
from app.dependencies import AuthCtx, ParsedFilters, ProjectCtx, require_key_type
from app.engines import query_engine
from app.models.requests import InsertRowRequest, RpcCallRequest, UpdateRowRequest
from app.models.responses import DataResponse, Meta

router = APIRouter(prefix="/db", tags=["SQL Database"])
logger = logging.getLogger(__name__)


@router.get("/{project_id}/{table}")
async def list_rows(
    project_id: str,
    table: str,
    ctx: ProjectCtx = Depends(),
    auth: AuthCtx = Depends(),
    filters: ParsedFilters = Depends(),
    select: str = Query(default="*", alias="select"),
    order: str | None = Query(default=None),
    order_dir: str = Query(default="asc"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=10000),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")
    await set_tenant_session(db, ctx["db_schema"])
    rows, total = await query_engine.list_rows(
        db,
        ctx["db_schema"],
        table,
        select_cols=select,
        filters=filters,
        order_col=order,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
        auth_ctx=auth,
    )
    return {"data": rows, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{project_id}/{table}/{row_id}")
async def get_row(
    project_id: str,
    table: str,
    row_id: str,
    ctx: ProjectCtx = Depends(),
    auth: AuthCtx = Depends(),
    select: str = Query(default="*", alias="select"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await set_tenant_session(db, ctx["db_schema"])
    row = await query_engine.get_row(db, ctx["db_schema"], table, row_id, select_cols=select)
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"data": row}


@router.post("/{project_id}/{table}")
async def insert_row(
    project_id: str,
    table: str,
    body: InsertRowRequest,
    auth: AuthCtx = Depends(),
    ctx: ProjectCtx = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await set_tenant_session(db, ctx["db_schema"])
    if isinstance(body.data, list):
        if len(body.data) > 1000:
            raise HTTPException(status_code=400, detail="Too many rows in batch insert")
        async with db.begin():
            rows = await query_engine.insert_row(db, ctx["db_schema"], table, body.data)
        return {"data": rows, "meta": {"count": len(rows)}}

    async with db.begin():
        row = await query_engine.insert_row(db, ctx["db_schema"], table, body.data)
    return {"data": row}


@router.patch("/{project_id}/{table}/{row_id}")
async def update_row(
    project_id: str,
    table: str,
    row_id: str,
    body: UpdateRowRequest,
    auth: AuthCtx = Depends(),
    ctx: ProjectCtx = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await set_tenant_session(db, ctx["db_schema"])
    row = await query_engine.update_row(db, ctx["db_schema"], table, row_id, body.data)
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"data": row}


@router.delete("/{project_id}/{table}/{row_id}")
async def delete_row(
    project_id: str,
    table: str,
    row_id: str,
    auth: AuthCtx = Depends(),
    ctx: ProjectCtx = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await set_tenant_session(db, ctx["db_schema"])
    deleted = await query_engine.delete_row(db, ctx["db_schema"], table, row_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"data": {"deleted": True, "id": row_id}}


@router.post("/{project_id}/rpc/{fn_name}")
async def call_rpc(
    project_id: str,
    fn_name: str,
    body: RpcCallRequest,
    ctx: ProjectCtx = Depends(require_key_type("service")),
    auth: AuthCtx = Depends(),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Call a PostgreSQL function defined in the project schema."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    schema = ctx["db_schema"]
    # Validate fn_name
    if not fn_name.isidentifier():
        raise HTTPException(status_code=400, detail="Invalid function name")

    from sqlalchemy import text
    params = body.args
    # Validate argument names
    for k in params.keys():
        if not k.isidentifier():
            raise HTTPException(status_code=400, detail=f"Invalid argument name: {k}")
    param_list = ", ".join(f":{k}" for k in params)
    # Ensure tenant search_path/role for this transaction
    await set_tenant_session(db, schema)
    result = await db.execute(
        text(f'SELECT * FROM "{schema}"."{fn_name}"({param_list})'),
        params,
    )
    rows = [dict(r._mapping) for r in result]
    return {"data": rows}