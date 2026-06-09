# backend/app/api/internal/sql_browse.py
"""
Internal-only endpoints for the dashboard to browse SQL (PostgreSQL) data.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db, engine, set_tenant_connection, set_tenant_session

router = APIRouter(tags=["Internal SQL Browse"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\-]*$")


def _validate_identifier(name: str, label: str = "identifier") -> None:
    if not _IDENTIFIER_RE.match(name):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {name!r}")


def _serialize_rows(rows: list[dict]) -> list[dict]:
    """Convert non-JSON-serializable types to safe values."""
    serialized = []
    for row in rows:
        clean = {}
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                clean[k] = v.isoformat()
            elif isinstance(v, bytes):
                clean[k] = v.hex()
            else:
                clean[k] = v
        serialized.append(clean)
    return serialized


def _is_cached_statement_error(exc: Exception) -> bool:
    """Check if the exception is an asyncpg InvalidCachedStatementError."""
    return "InvalidCachedStatementError" in type(exc).__name__ or (
        hasattr(exc, "orig") and "InvalidCachedStatementError" in type(exc.orig).__name__
    ) or "cached statement plan is invalid" in str(exc)


async def _execute_with_retry(db: AsyncSession, stmt: text, params: dict | None = None):
    """
    Execute a statement, retrying once if asyncpg invalidates its prepared
    statement cache after a schema change (ADD/DROP COLUMN, etc.).
    asyncpg automatically clears the cache on this error, so the retry succeeds.
    """
    try:
        return await db.execute(stmt, params or {})
    except Exception as exc:
        if _is_cached_statement_error(exc):
            logger.info("Retrying after cached statement invalidation")
            # Roll back the failed statement so the session is clean
            await db.rollback()
            return await db.execute(stmt, params or {})
        raise


@router.get("/projects/{project_id}/sql/tables", dependencies=[InternalGuard])
async def list_tables(
    project_id: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_identifier(db_schema, "schema")
    await set_tenant_session(db, db_schema)
    result = await _execute_with_retry(db, text("""
            SELECT
                t.table_name,
                COALESCE(s.n_live_tup, 0)::bigint AS row_count
            FROM information_schema.tables t
            LEFT JOIN pg_stat_user_tables s
                ON s.schemaname = t.table_schema
                AND s.relname = t.table_name
            WHERE t.table_schema = :schema
              AND t.table_type = 'BASE TABLE'
              AND t.table_name NOT LIKE '\_%'
            ORDER BY t.table_name
        """), {"schema": db_schema})
    tables = [{"name": row["table_name"], "rows": row["row_count"]} for row in result.mappings()]
    return {"data": {"tables": tables}}


@router.get("/projects/{project_id}/sql/tables/{table}/columns", dependencies=[InternalGuard])
async def list_columns(
    project_id: str,
    table: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_identifier(db_schema, "schema")
    _validate_identifier(table, "table")
    await set_tenant_session(db, db_schema)

    result = await _execute_with_retry(db, text("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            ORDER BY ordinal_position
        """), {"schema": db_schema, "table": table})
    columns = [dict(row) for row in result.mappings()]
    return {"data": {"columns": columns}}


@router.get("/projects/{project_id}/sql/tables/{table}/rows", dependencies=[InternalGuard])
async def list_rows(
    project_id: str,
    table: str,
    db_schema: str = Query(...),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order_col: str | None = Query(default=None),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_identifier(db_schema, "schema")
    _validate_identifier(table, "table")
    if order_col:
        _validate_identifier(order_col, "order column")

    await set_tenant_session(db, db_schema)

    order_clause = (
        f'ORDER BY "{order_col}" {order_dir.upper()}'
        if order_col
        else "ORDER BY created_at DESC NULLS LAST"
    )

    rows_result = await _execute_with_retry(
        db,
        text(f'SELECT * FROM "{db_schema}"."{table}" {order_clause} LIMIT :limit OFFSET :offset'),
        {"limit": limit, "offset": offset},
    )
    count_result = await _execute_with_retry(
        db,
        text(f'SELECT COUNT(*) FROM "{db_schema}"."{table}"'),
    )

    rows = [dict(r._mapping) for r in rows_result]
    total = count_result.scalar() or 0
    return {"data": {"rows": _serialize_rows(rows), "total": total, "limit": limit, "offset": offset}}


class RunQueryRequest(BaseModel):
    query: str
    db_schema: str


@router.post("/projects/{project_id}/sql/query", dependencies=[InternalGuard])
async def run_query(
    project_id: str,
    body: RunQueryRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Execute a read-only SELECT query scoped to the project's schema.
    Uses a raw asyncpg connection so SET LOCAL search_path persists
    for the duration of the transaction.
    """
    query = body.query.strip()

    # Strip leading SQL comments before the keyword check
    query_no_comments = re.sub(r"(--[^\n]*\n?|/\*.*?\*/)", "", query, flags=re.DOTALL).strip()

    if not query_no_comments.upper().startswith("SELECT"):
        raise HTTPException(
            status_code=400,
            detail="Only SELECT queries are permitted from the dashboard.",
        )

    db_schema = body.db_schema
    _validate_identifier(db_schema, "schema")

    async def _run():
        async with engine.connect() as conn:
            await conn.execute(text("BEGIN"))
            await set_tenant_connection(conn, db_schema)
            result = await conn.execute(text(query_no_comments))
            rows = [dict(r._mapping) for r in result]
            await conn.execute(text("ROLLBACK"))
        return rows

    try:
        rows = await _run()
    except Exception as exc:
        if _is_cached_statement_error(exc):
            logger.info("Retrying query after cached statement invalidation")
            try:
                rows = await _run()
            except Exception as exc2:
                logger.warning("Query error for project %s: %s", project_id, exc2)
                raise HTTPException(status_code=400, detail=str(exc2))
        else:
            logger.warning("Query error for project %s: %s", project_id, exc)
            raise HTTPException(status_code=400, detail=str(exc))

    return {"data": {"rows": _serialize_rows(rows), "total": len(rows)}}


# ─── Row CRUD ─────────────────────────────────────────────────────────────────

class InsertRowRequest(BaseModel):
    db_schema: str
    data: dict[str, Any]


class UpdateRowRequest(BaseModel):
    db_schema: str
    data: dict[str, Any]


@router.post("/projects/{project_id}/sql/tables/{table}/rows", status_code=201, dependencies=[InternalGuard])
async def insert_row(
    project_id: str,
    table: str,
    body: InsertRowRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_identifier(body.db_schema, "schema")
    _validate_identifier(table, "table")

    await set_tenant_session(db, body.db_schema)

    data = body.data
    if not data:
        raise HTTPException(status_code=400, detail="No data provided")

    cols = ", ".join(f'"{k}"' for k in data)
    vals = ", ".join(f":val_{k}" for k in data)
    params = {f"val_{k}": v for k, v in data.items()}

    result = await _execute_with_retry(
        db,
        text(f'INSERT INTO "{body.db_schema}"."{table}" ({cols}) VALUES ({vals}) RETURNING *'),
        params,
    )
    row = result.mappings().first()
    await db.commit()
    return {"data": _serialize_rows([dict(row)])[0] if row else {}}


@router.patch("/projects/{project_id}/sql/tables/{table}/rows/{row_id}", dependencies=[InternalGuard])
async def update_row(
    project_id: str,
    table: str,
    row_id: str,
    body: UpdateRowRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_identifier(body.db_schema, "schema")
    _validate_identifier(table, "table")

    await set_tenant_session(db, body.db_schema)

    data = body.data
    if not data:
        raise HTTPException(status_code=400, detail="No data provided")

    set_clause = ", ".join(f'"{k}" = :upd_{k}' for k in data)
    params = {f"upd_{k}": v for k, v in data.items()}
    params["row_id"] = row_id

    result = await _execute_with_retry(
        db,
        text(f'UPDATE "{body.db_schema}"."{table}" SET {set_clause} WHERE id = :row_id RETURNING *'),
        params,
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    await db.commit()
    return {"data": _serialize_rows([dict(row)])[0]}


@router.delete("/projects/{project_id}/sql/tables/{table}/rows/{row_id}", dependencies=[InternalGuard])
async def delete_row(
    project_id: str,
    table: str,
    row_id: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    _validate_identifier(db_schema, "schema")
    _validate_identifier(table, "table")

    await set_tenant_session(db, db_schema)

    result = await _execute_with_retry(
        db,
        text(f'DELETE FROM "{db_schema}"."{table}" WHERE id = :row_id RETURNING id'),
        {"row_id": row_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Row not found")
    await db.commit()
    return {"data": {"deleted": True, "id": row_id}}


# ─── Truncate table ───────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/tables/{table}/truncate", dependencies=[InternalGuard])
async def truncate_table(
    project_id: str,
    table: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _validate_identifier(db_schema, "schema")
    _validate_identifier(table, "table")

    await set_tenant_session(db, db_schema)

    if table.startswith("_"):
        raise HTTPException(status_code=400, detail="Cannot truncate reserved tables")

    await db.execute(text(f'TRUNCATE TABLE "{db_schema}"."{table}" RESTART IDENTITY'))
    await db.commit()
    logger.info("Truncated table: %s.%s (project: %s)", db_schema, table, project_id)
    return {"data": {"table": table, "truncated": True}}


# ─── Drop a single column ─────────────────────────────────────────────────────

@router.delete(
    "/projects/{project_id}/tables/{table}/columns/{column}",
    dependencies=[InternalGuard],
)
async def drop_column_endpoint(
    project_id: str,
    table: str,
    column: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _validate_identifier(db_schema, "schema")
    _validate_identifier(table, "table")
    _validate_identifier(column, "column")

    await set_tenant_session(db, db_schema)

    PROTECTED = {"id", "created_at", "updated_at"}
    if column in PROTECTED:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot drop system column '{column}'",
        )

    await db.execute(
        text(f'ALTER TABLE "{db_schema}"."{table}" DROP COLUMN IF EXISTS "{column}"')
    )
    await db.commit()
    logger.info("Dropped column %s from %s.%s (project: %s)", column, db_schema, table, project_id)
    return {"data": {"column": column, "dropped": True}}

 
# ─── Foreign keys ─────────────────────────────────────────────────────────────
 
@router.get("/projects/{project_id}/sql/relationships", dependencies=[InternalGuard])
async def list_relationships(
    project_id: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return all FK relationships in the project schema."""
    _validate_identifier(db_schema, "schema")
    await set_tenant_session(db, db_schema)
    from app.provisioner.sql_provisioner import get_foreign_keys
    fks = await get_foreign_keys(db, db_schema)
    return {"data": {"relationships": fks}}
 
