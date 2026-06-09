# backend/app/api/internal/functions_browse.py
"""
Internal-only endpoints for the dashboard to manage edge functions.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import json
import logging
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(tags=["Internal Functions"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


def _serialize_fn(row: dict) -> dict:
    r = dict(row)
    for field in ("created_at", "updated_at", "last_invoked_at"):
        if r.get(field) and hasattr(r[field], "isoformat"):
            r[field] = r[field].isoformat()
    return r


# ─── List functions ───────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/functions", dependencies=[InternalGuard])
async def list_functions(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = await db.execute(
            text("""
                SELECT id, project_id, name, description, endpoint_url, method,
                       timeout_ms, is_active, invoke_count, last_invoked_at,
                       created_at, updated_at
                FROM edge_functions
                WHERE project_id = :project_id
                ORDER BY created_at DESC
            """),
            {"project_id": project_id},
        )
        fns = [_serialize_fn(dict(r)) for r in result.mappings()]
        return {"data": {"functions": fns, "total": len(fns)}}
    except Exception as e:
        logger.warning("edge_functions table may not exist yet: %s", e)
        return {"data": {"functions": [], "total": 0}}


# ─── Get stats ────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/functions/stats", dependencies=[InternalGuard])
async def get_function_stats(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = await db.execute(
            text("""
                SELECT
                    COUNT(*)                                       AS total,
                    COUNT(*) FILTER (WHERE is_active = true)      AS active,
                    COUNT(*) FILTER (WHERE is_active = false)     AS inactive,
                    COALESCE(SUM(invoke_count), 0)                AS total_invocations
                FROM edge_functions
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        row = result.mappings().first()
        return {"data": dict(row) if row else {"total": 0, "active": 0, "inactive": 0, "total_invocations": 0}}
    except Exception:
        return {"data": {"total": 0, "active": 0, "inactive": 0, "total_invocations": 0}}


# ─── Get single function ──────────────────────────────────────────────────────

@router.get("/projects/{project_id}/functions/{function_id}", dependencies=[InternalGuard])
async def get_function(
    project_id: str,
    function_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT id, project_id, name, description, endpoint_url, method,
                   timeout_ms, is_active, invoke_count, last_invoked_at,
                   created_at, updated_at
            FROM edge_functions
            WHERE id = :id AND project_id = :project_id
        """),
        {"id": function_id, "project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Function not found")
    return {"data": _serialize_fn(dict(row))}


# ─── Create function ──────────────────────────────────────────────────────────

class CreateFunctionRequest(BaseModel):
    projectId: str
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    endpoint_url: str
    method: str = "POST"
    timeout_ms: int = Field(default=30000, ge=1000, le=300000)


@router.post("/projects/{project_id}/functions", status_code=201, dependencies=[InternalGuard])
async def create_function(
    project_id: str,
    body: CreateFunctionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    # Check uniqueness
    existing = await db.execute(
        text("SELECT id FROM edge_functions WHERE project_id = :p AND name = :n"),
        {"p": project_id, "n": body.name},
    )
    if existing.first():
        raise HTTPException(status_code=409, detail=f"A function named '{body.name}' already exists")

    fn_id = str(uuid.uuid4())
    method = body.method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(status_code=400, detail="Invalid HTTP method")

    await db.execute(
        text("""
            INSERT INTO edge_functions
                (id, project_id, name, description, endpoint_url, method, timeout_ms, is_active)
            VALUES
                (:id, :project_id, :name, :description, :endpoint_url, :method, :timeout_ms, true)
        """),
        {
            "id": fn_id,
            "project_id": project_id,
            "name": body.name,
            "description": body.description,
            "endpoint_url": body.endpoint_url,
            "method": method,
            "timeout_ms": body.timeout_ms,
        },
    )
    await db.commit()
    return {"data": {"id": fn_id, "name": body.name, "created": True}}


# ─── Update function ──────────────────────────────────────────────────────────

class UpdateFunctionRequest(BaseModel):
    projectId: str | None = None
    description: str | None = None
    endpoint_url: str | None = None
    method: str | None = None
    timeout_ms: int | None = None
    is_active: bool | None = None


@router.patch("/projects/{project_id}/functions/{function_id}", dependencies=[InternalGuard])
async def update_function(
    project_id: str,
    function_id: str,
    body: UpdateFunctionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if body.description is not None:
        updates["description"] = body.description
    if body.endpoint_url is not None:
        updates["endpoint_url"] = body.endpoint_url
    if body.method is not None:
        m = body.method.upper()
        if m not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise HTTPException(status_code=400, detail="Invalid HTTP method")
        updates["method"] = m
    if body.timeout_ms is not None:
        updates["timeout_ms"] = body.timeout_ms
    if body.is_active is not None:
        updates["is_active"] = body.is_active

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates["updated_at"] = "NOW()"
    set_parts = []
    params: dict[str, Any] = {"fn_id": function_id, "project_id": project_id}
    for k, v in updates.items():
        if v == "NOW()":
            set_parts.append(f"{k} = NOW()")
        else:
            set_parts.append(f"{k} = :{k}")
            params[k] = v

    set_clause = ", ".join(set_parts)
    result = await db.execute(
        text(f"UPDATE edge_functions SET {set_clause} WHERE id = :fn_id AND project_id = :project_id RETURNING id"),
        params,
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Function not found")
    await db.commit()
    return {"data": {"id": function_id, "updated": True}}


# ─── Delete function ──────────────────────────────────────────────────────────

@router.delete("/projects/{project_id}/functions/{function_id}", dependencies=[InternalGuard])
async def delete_function(
    project_id: str,
    function_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("DELETE FROM edge_functions WHERE id = :id AND project_id = :project_id RETURNING id"),
        {"id": function_id, "project_id": project_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Function not found")
    await db.commit()
    return {"data": {"id": function_id, "deleted": True}}


# ─── Test invocation ──────────────────────────────────────────────────────────

class TestFunctionRequest(BaseModel):
    projectId: str
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/projects/{project_id}/functions/{function_id}/test", dependencies=[InternalGuard])
async def test_function(
    project_id: str,
    function_id: str,
    body: TestFunctionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Invoke a function and return the result. Logs the invocation."""
    result = await db.execute(
        text("""
            SELECT id, name, endpoint_url, method, timeout_ms, is_active
            FROM edge_functions
            WHERE id = :id AND project_id = :project_id
        """),
        {"id": function_id, "project_id": project_id},
    )
    fn = result.mappings().first()
    if not fn:
        raise HTTPException(status_code=404, detail="Function not found")
    if not fn["is_active"]:
        raise HTTPException(status_code=400, detail="Function is inactive — enable it before testing")

    timeout_s = (fn["timeout_ms"] or 30000) / 1000
    method = (fn["method"] or "POST").upper()
    start = time.monotonic()
    status_code: int | None = None
    response_body: str | None = None
    error: str | None = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.request(
                method,
                fn["endpoint_url"],
                json=body.payload if method not in {"GET", "DELETE"} else None,
                params=body.payload if method in {"GET", "DELETE"} else None,
                headers={"Content-Type": "application/json"},
            )
            status_code = resp.status_code
            try:
                response_body = resp.text[:4000]  # cap at 4KB
            except Exception:
                response_body = ""
            success = 200 <= resp.status_code < 300
    except httpx.TimeoutException:
        error = f"Request timed out after {int(timeout_s)}s"
    except httpx.ConnectError as e:
        error = f"Connection failed: {str(e)}"
    except Exception as e:
        error = f"Unexpected error: {str(e)}"

    duration_ms = int((time.monotonic() - start) * 1000)

    # Parse response JSON if possible
    parsed_response = None
    if response_body:
        try:
            parsed_response = json.loads(response_body)
        except Exception:
            parsed_response = response_body

    # Log the invocation
    log_id = str(uuid.uuid4())
    try:
        await db.execute(
            text("""
                INSERT INTO edge_function_logs
                    (id, project_id, function_id, function_name, status_code,
                     duration_ms, request_payload, response_body, error)
                VALUES
                    (:id, :project_id, :fn_id, :fn_name, :status_code,
                     :duration_ms, :request_payload, :response_body, :error)
            """),
            {
                "id": log_id,
                "project_id": project_id,
                "fn_id": function_id,
                "fn_name": fn["name"],
                "status_code": status_code,
                "duration_ms": duration_ms,
                "request_payload": json.dumps(body.payload)[:2000],
                "response_body": response_body[:2000] if response_body else None,
                "error": error,
            },
        )
        # Bump invoke count + last_invoked_at
        await db.execute(
            text("""
                UPDATE edge_functions
                SET invoke_count = invoke_count + 1,
                    last_invoked_at = NOW()
                WHERE id = :id
            """),
            {"id": function_id},
        )
        await db.commit()
    except Exception as log_err:
        logger.warning("Failed to log function invocation: %s", log_err)

    return {
        "data": {
            "status_code": status_code,
            "response": parsed_response,
            "error": error,
            "duration_ms": duration_ms,
            "success": success,
        }
    }


# ─── Invocation logs ──────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/functions/{function_id}/logs", dependencies=[InternalGuard])
async def get_function_logs(
    project_id: str,
    function_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = await db.execute(
            text("""
                SELECT id, function_name, status_code, duration_ms,
                       request_payload, response_body, error, created_at
                FROM edge_function_logs
                WHERE function_id = :fn_id AND project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"fn_id": function_id, "project_id": project_id, "limit": limit},
        )
        logs = []
        for r in result.mappings():
            row = dict(r)
            if row.get("created_at") and hasattr(row["created_at"], "isoformat"):
                row["created_at"] = row["created_at"].isoformat()
            logs.append(row)
        return {"data": {"logs": logs}}
    except Exception as e:
        logger.warning("edge_function_logs table may not exist: %s", e)
        return {"data": {"logs": []}}