import logging
import re
from typing import Any

from sqlalchemy import text
from app.db.postgres import set_tenant_session
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permissions import AuthContext

logger = logging.getLogger(__name__)

ALLOWED_OPERATORS = {
    "eq": "=",
    "neq": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
    "ilike": "ILIKE",
    "in": "IN",
    "is": "IS",
}


def _build_where_clause(
    filters: list[tuple[str, str, Any]],
    params: dict[str, Any],
    extra_condition: str | None = None,
) -> str:
    parts: list[str] = []

    for i, (col, op, val) in enumerate(filters):
        if op not in ALLOWED_OPERATORS:
            raise ValueError(f"Unknown operator: {op}")

        if "." in col:
            table_part, col_part = col.split(".", 1)
            _validate_identifier(table_part)
            _validate_identifier(col_part)
        else:
            _validate_identifier(col)

        sql_op = ALLOWED_OPERATORS[op]
        param_name = f"filter_{i}"

        if op == "in":
            if not isinstance(val, list):
                raise ValueError("'in' operator requires a list value")
            # Use positional params for IN
            in_params = {f"{param_name}_{j}": v for j, v in enumerate(val)}
            params.update(in_params)
            placeholders = ", ".join(f":{k}" for k in in_params)
            parts.append(f'"{col}" IN ({placeholders})')
        elif op == "is":
            if val is None:
                parts.append(f'"{col}" IS NULL')
            else:
                parts.append(f'"{col}" IS NOT NULL')
        else:
            params[param_name] = val
            parts.append(f'"{col}" {sql_op} :{param_name}')

    if extra_condition:
        parts.append(f"({extra_condition})")

    return " AND ".join(parts) if parts else "TRUE"


async def list_rows(
    session: AsyncSession,
    schema: str,
    table: str,
    *,
    select_cols: str = "*",
    filters: list[tuple[str, str, Any]] | None = None,
    order_col: str | None = None,
    order_dir: str = "asc",
    limit: int = 100,
    offset: int = 0,
    auth_ctx: AuthContext | None = None,
    extra_condition: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    _validate_identifier(schema)
    _validate_identifier(table)
    _validate_order_dir(order_dir)

    params: dict[str, Any] = {}
    # Constrain transaction to the tenant schema and (optionally) drop privileges
    await set_tenant_session(session, schema)
    where = _build_where_clause(filters or [], params, extra_condition)

    # Validate requested select and order columns against actual table schema
    # Fetch column names for the target table
    col_result = await session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            """
        ),
        {"schema": schema, "table": table},
    )
    valid_cols = {r[0] for r in col_result.fetchall()}

    # Validate order_col if provided
    if order_col and order_col not in valid_cols:
        raise ValueError(f"Invalid order column: {order_col}")

    # Validate select columns: only allow '*' or an explicit subset of valid columns
    if select_cols.strip() != "*":
        requested = [c.strip() for c in select_cols.split(",")]
        for c in requested:
            # support table-qualified column like 'table.col' by taking last part
            col_name = c.split(".")[-1]
            if col_name not in valid_cols:
                raise ValueError(f"Invalid select column: {c}")


    order_clause = ""
    if order_col:
        _validate_identifier(order_col)
        order_clause = f'ORDER BY "{order_col}" {order_dir.upper()}'

    # Build safe select columns (we already validated names against schema)
    safe_select = _safe_select(select_cols)

    query = f"""
        SELECT {safe_select} FROM "{schema}"."{table}"
        WHERE {where}
        {order_clause}
        LIMIT :limit OFFSET :offset
    """
    count_query = f"""
        SELECT COUNT(*) FROM "{schema}"."{table}"
        WHERE {where}
    """
    params["limit"] = min(limit, 1000)
    params["offset"] = offset

    rows_result = await session.execute(text(query), params)
    count_result = await session.execute(text(count_query), {k: v for k, v in params.items() if k not in ("limit", "offset")})

    rows = [dict(r._mapping) for r in rows_result]
    total = count_result.scalar() or 0
    return rows, total


async def get_row(
    session: AsyncSession,
    schema: str,
    table: str,
    row_id: Any,
    *,
    select_cols: str = "*",
    extra_condition: str | None = None,
) -> dict[str, Any] | None:
    _validate_identifier(schema)
    _validate_identifier(table)

    # Constrain transaction to tenant schema
    await set_tenant_session(session, schema)

    valid_cols = await _get_table_columns(session, schema, table)
    if select_cols.strip() != "*":
        requested = [c.strip() for c in select_cols.split(",")]
        for c in requested:
            col_name = c.split(".")[-1]
            if col_name not in valid_cols:
                raise ValueError(f"Invalid select column: {c}")

    safe_select = _safe_select(select_cols)
    params: dict[str, Any] = {"row_id": row_id}
    where = "id = :row_id"
    if extra_condition:
        where += f" AND ({extra_condition})"

    result = await session.execute(
        text(f'SELECT {safe_select} FROM "{schema}"."{table}" WHERE {where} LIMIT 1'),
        params,
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def insert_row(
    session: AsyncSession,
    schema: str,
    table: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    _validate_identifier(schema)
    _validate_identifier(table)
    await set_tenant_session(session, schema)
    valid_cols = await _get_table_columns(session, schema, table)
    # Support single-row insert or bulk insert when `data` is a list of dicts.
    if isinstance(data, list):
        if not data:
            return []
        # Ensure all rows have the same set of columns
        first_keys = list(data[0].keys())
        for idx, item in enumerate(data):
            if list(item.keys()) != first_keys:
                raise ValueError("All rows in a bulk insert must have identical keys and order")

        for k in first_keys:
            _validate_identifier(k)
            if k not in valid_cols:
                raise ValueError(f"Invalid column name in insert: {k}")

        cols = ", ".join(f'"{k}"' for k in first_keys)
        values_parts: list[str] = []
        params: dict[str, Any] = {}
        for i, item in enumerate(data):
            part = []
            for k in first_keys:
                param_name = f"{k}_{i}"
                part.append(f":{param_name}")
                params[param_name] = item[k]
            values_parts.append(f"({', '.join(part)})")

        values_sql = ", ".join(values_parts)
        query = text(f'INSERT INTO "{schema}"."{table}" ({cols}) VALUES {values_sql} RETURNING *')
        result = await session.execute(query, params)
        return [dict(r._mapping) for r in result]

    # Single row insert (existing behavior)
    for k in data.keys():
        _validate_identifier(k)
        if k not in valid_cols:
            raise ValueError(f"Invalid column name in insert: {k}")

    cols = ", ".join(f'"{k}"' for k in data)
    vals = ", ".join(f":{k}" for k in data)
    result = await session.execute(
        text(f'INSERT INTO "{schema}"."{table}" ({cols}) VALUES ({vals}) RETURNING *'),
        data,
    )
    row = result.mappings().first()
    return dict(row) if row else {}


async def update_row(
    session: AsyncSession,
    schema: str,
    table: str,
    row_id: Any,
    data: dict[str, Any],
    *,
    extra_condition: str | None = None,
) -> dict[str, Any] | None:
    _validate_identifier(schema)
    _validate_identifier(table)
    await set_tenant_session(session, schema)

    valid_cols = await _get_table_columns(session, schema, table)
    for k in data.keys():
        _validate_identifier(k)
        if k not in valid_cols:
            raise ValueError(f"Invalid column name in update: {k}")

    set_clause = ", ".join(f'"{k}" = :upd_{k}' for k in data)
    params = {f"upd_{k}": v for k, v in data.items()}
    params["row_id"] = row_id

    where = "id = :row_id"
    if extra_condition:
        where += f" AND ({extra_condition})"

    result = await session.execute(
        text(f'UPDATE "{schema}"."{table}" SET {set_clause} WHERE {where} RETURNING *'),
        params,
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def delete_row(
    session: AsyncSession,
    schema: str,
    table: str,
    row_id: Any,
    *,
    extra_condition: str | None = None,
) -> bool:
    _validate_identifier(schema)
    _validate_identifier(table)
    await set_tenant_session(session, schema)

    params: dict[str, Any] = {"row_id": row_id}
    where = "id = :row_id"
    if extra_condition:
        where += f" AND ({extra_condition})"

    result = await session.execute(
        text(f'DELETE FROM "{schema}"."{table}" WHERE {where}'),
        params,
    )
    return (result.rowcount or 0) > 0


def _validate_identifier(name: str) -> None:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        raise ValueError(f"Invalid identifier: {name!r}")


async def _get_table_columns(session: AsyncSession, schema: str, table: str) -> set[str]:
    result = await session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            """
        ),
        {"schema": schema, "table": table},
    )
    return {row[0] for row in result}


def _validate_order_dir(direction: str) -> None:
    if direction.lower() not in ("asc", "desc"):
        raise ValueError(f"Invalid order direction: {direction}")


def _safe_select(select_cols: str) -> str:
    if select_cols.strip() == "*":
        return "*"
    cols = [c.strip() for c in select_cols.split(",")]
    safe = []
    for col in cols:
        if col.replace("_", "").replace(".", "").isalnum():
            safe.append(f'"{col}"')
    return ", ".join(safe) if safe else "*"