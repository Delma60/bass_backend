# backend/app/api/v1/db/router.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db, set_tenant_session
from app.dependencies import AuthCtx, ParsedFilters, ProjectCtx, require_key_type
from app.engines import query_engine
from app.engines.permission_engine import check_permission, inject_auth_uid
from app.models.requests import InsertRowRequest, RpcCallRequest, UpdateRowRequest
from app.tasks.usage_sync import record_usage

router = APIRouter(prefix="/db", tags=["SQL Database"])
logger = logging.getLogger(__name__)

async def _get_sql_condition(project_id: str, table: str, operation: str, auth: AuthCtx) -> str | None:
    """Helper to evaluate permissions and format the SQL condition string."""
    condition = await check_permission(project_id, table, operation, "sql", auth)
    if condition and auth.uid:
        condition = inject_auth_uid(condition, auth.uid)
    return condition

@router.get("/{project_id}/{table}")
async def list_rows(
    project_id: str,
    table: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
    filters: ParsedFilters,
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
    condition = await _get_sql_condition(project_id, table, "list", auth)
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
        extra_condition=condition,
    )
    record_usage.delay(project_id, "db_reads", 1)
    return {"data": rows, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/{project_id}/{table}/{row_id}")
async def get_row(
    project_id: str,
    table: str,
    row_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
    select: str = Query(default="*", alias="select"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await set_tenant_session(db, ctx["db_schema"])
    condition = await _get_sql_condition(project_id, table, "get", auth)
    row = await query_engine.get_row(db, ctx["db_schema"], table, row_id, select_cols=select, extra_condition=condition)
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    record_usage.delay(project_id, "db_reads", 1)
    return {"data": row}


@router.post("/{project_id}/{table}", status_code=201)
async def insert_row(
    project_id: str,
    table: str,
    body: InsertRowRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")
    
    await set_tenant_session(db, ctx["db_schema"])
    await _get_sql_condition(project_id, table, "insert", auth)

    if isinstance(body.data, list):
        # Pydantic should enforce max items, but double-check here
        if len(body.data) > 1000:
            raise HTTPException(status_code=400, detail="Too many rows in batch insert")
        # Execute bulk insert inside a transaction so the operation is atomic
        async with db.begin():
            rows = await query_engine.insert_row(db, ctx["db_schema"], table, body.data)
        record_usage.delay(project_id, "db_writes", len(rows))
        return {"data": rows, "meta": {"count": len(rows)}}

    # Single row
    async with db.begin():
        row = await query_engine.insert_row(db, ctx["db_schema"], table, body.data)
    record_usage.delay(project_id, "db_writes", 1)
    return {"data": row}


@router.patch("/{project_id}/{table}/{row_id}")
async def update_row(
    project_id: str,
    table: str,
    row_id: str,
    body: UpdateRowRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")
    
    await set_tenant_session(db, ctx["db_schema"])
    condition = await _get_sql_condition(project_id, table, "update", auth)
    row = await query_engine.update_row(db, ctx["db_schema"], table, row_id, body.data, extra_condition=condition)
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    record_usage.delay(project_id, "db_writes", 1)
    return {"data": row}


@router.delete("/{project_id}/{table}/{row_id}")
async def delete_row(
    project_id: str,
    table: str,
    row_id: str,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    await set_tenant_session(db, ctx["db_schema"])
    condition = await _get_sql_condition(project_id, table, "delete", auth)
    deleted = await query_engine.delete_row(db, ctx["db_schema"], table, row_id, extra_condition=condition)
    if not deleted:
        raise HTTPException(status_code=404, detail="Row not found")
    record_usage.delay(project_id, "db_writes", 1)
    return {"data": {"deleted": True, "id": row_id}}


@router.post("/{project_id}/rpc/{fn_name}")
async def call_rpc(
    project_id: str,
    fn_name: str,
    body: RpcCallRequest,
    auth: AuthCtx,
    ctx: dict[str, Any] = Depends(require_key_type("service")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Call a PostgreSQL function defined in the project schema."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    if not fn_name.isidentifier():
        raise HTTPException(status_code=400, detail="Invalid function name")

    schema = ctx["db_schema"]
    params = body.args
    for k in params.keys():
        if not k.isidentifier():
            raise HTTPException(status_code=400, detail=f"Invalid argument name: {k}")
    param_list = ", ".join(f":{k}" for k in params)

    # Ensure transaction/search_path is limited to the tenant schema
    await set_tenant_session(db, schema)

    result = await db.execute(
        text(f'SELECT * FROM "{schema}"."{fn_name}"({param_list})'),
        params,
    )
    rows = [dict(r._mapping) for r in result]
    
    # Track the RPC call as a read
    record_usage.delay(project_id, "db_reads", 1)
    
    return {"data": rows}