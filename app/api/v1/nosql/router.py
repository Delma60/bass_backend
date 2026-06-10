# backend/app/api/v1/nosql/router.py
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db.mongo import get_project_db
from app.dependencies import AuthCtx, ProjectCtx
from app.engines import nosql_engine
from app.models.requests import (
    AggregationRequest,
    InsertDocumentRequest,
    KVBatchRequest,
    KVSetRequest,
    UpdateDocumentRequest,
)
from app.engines.permission_engine import check_permission, inject_auth_uid
from app.tasks.usage_sync import record_usage

async def _get_nosql_condition(project_id: str, collection: str, operation: str, auth: AuthCtx) -> dict[str, Any] | None:
    """Helper to evaluate permissions and format the NoSQL condition dict."""
    condition = await check_permission(project_id, collection, operation, "nosql", auth)
    if condition and auth.uid:
        condition = inject_auth_uid(condition, auth.uid)
    return condition if isinstance(condition, dict) else None

router = APIRouter(prefix="/nosql", tags=["NoSQL Database"])
logger = logging.getLogger(__name__)


# ─── Collections ──────────────────────────────────────────────────────────────

@router.get("/{project_id}/collections/{collection}")
async def find_documents(
    project_id: str,
    collection: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
    limit: int = Query(default=100, ge=1, le=1000),
    skip: int = Query(default=0, ge=0),
    sort_field: str | None = Query(default=None),
    sort_dir: int = Query(default=-1, description="-1 desc, 1 asc"),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    sort = [(sort_field, sort_dir)] if sort_field else None

    filter_doc = await _get_nosql_condition(project_id, collection, "find", auth)
    docs, total = await nosql_engine.find_documents(
        db, collection, sort=sort, limit=limit, skip=skip,
        filter_doc=filter_doc,
    )
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": docs, "meta": {"count": total, "limit": limit, "skip": skip}}


@router.get("/{project_id}/collections/{collection}/{doc_id}")
async def get_document(
    project_id: str,
    collection: str,
    doc_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    condition = await _get_nosql_condition(project_id, collection, "get", auth)

    db = get_project_db(ctx["mongo_database"])
    doc = await nosql_engine.get_document(db, collection, doc_id, extra_condition=condition)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": doc}


@router.post("/{project_id}/collections/{collection}", status_code=201)
async def insert_document(
    project_id: str,
    collection: str,
    body: InsertDocumentRequest,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")
    await _get_nosql_condition(project_id, collection, "INSERT", auth)

    db = get_project_db(ctx["mongo_database"])

    if isinstance(body.data, list):
        ids = await nosql_engine.insert_many_documents(db, collection, body.data)
        record_usage.delay(project_id, "nosql_writes", len(ids))
        return {"data": {"inserted_ids": ids}, "meta": {"count": len(ids)}}

    doc = await nosql_engine.insert_document(db, collection, body.data)
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": doc}


@router.patch("/{project_id}/collections/{collection}/{doc_id}")
async def update_document(
    project_id: str,
    collection: str,
    doc_id: str,
    body: UpdateDocumentRequest,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    doc = await nosql_engine.update_document(db, collection, doc_id, body.update)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": doc}


@router.delete("/{project_id}/collections/{collection}/{doc_id}")
async def delete_document(
    project_id: str,
    collection: str,
    doc_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    deleted = await nosql_engine.delete_document(db, collection, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": {"deleted": True, "id": doc_id}}


@router.post("/{project_id}/collections/{collection}/aggregate")
async def aggregate_documents(
    project_id: str,
    collection: str,
    body: AggregationRequest,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    results = await nosql_engine.aggregate_documents(db, collection, body.pipeline)
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": results, "meta": {"count": len(results)}}


# ─── Key-Value ─────────────────────────────────────────────────────────────────

@router.get("/{project_id}/kv")
async def list_kv_keys(
    project_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
    prefix: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    entries = await nosql_engine.kv_list(db, prefix=prefix, limit=limit)
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": entries, "meta": {"count": len(entries)}}


@router.get("/{project_id}/kv/{key:path}")
async def get_kv(
    project_id: str,
    key: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    value = await nosql_engine.kv_get(db, key)
    if value is None:
        raise HTTPException(status_code=404, detail="Key not found")
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": {"key": key, "value": value}}


@router.put("/{project_id}/kv/{key:path}", status_code=201)
async def set_kv(
    project_id: str,
    key: str,
    body: KVSetRequest,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    await nosql_engine.kv_set(db, key, body.value, ttl=body.ttl)
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": {"key": key, "value": body.value}}


@router.delete("/{project_id}/kv/{key:path}")
async def delete_kv(
    project_id: str,
    key: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    deleted = await nosql_engine.kv_delete(db, key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": {"deleted": True, "key": key}}


@router.post("/{project_id}/kv/batch")
async def batch_kv(
    project_id: str,
    body: KVBatchRequest,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    """Batch get/set/delete operations."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    db = get_project_db(ctx["mongo_database"])
    results = []

    reads = 0
    writes = 0
    for op in body.operations:
        op_type = op.get("op", "").lower()
        key = op.get("key", "")
        if not key:
            results.append({"error": "Missing key"})
            continue

        if op_type == "get":
            value = await nosql_engine.kv_get(db, key)
            results.append({"key": key, "value": value})
            reads += 1
        elif op_type == "set":
            value = op.get("value")
            ttl = op.get("ttl")
            await nosql_engine.kv_set(db, key, value, ttl=ttl)
            results.append({"key": key, "set": True})
            writes += 1
        elif op_type == "delete":
            deleted = await nosql_engine.kv_delete(db, key)
            results.append({"key": key, "deleted": deleted})
            writes += 1
        else:
            results.append({"key": key, "error": f"Unknown op: {op_type}"})

    return {"data": results, "meta": {"count": len(results)}}