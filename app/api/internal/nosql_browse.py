# backend/app/api/internal/nosql_browse.py
"""
Internal-only endpoints for the dashboard to browse NoSQL data.
These are NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.db.mongo import get_project_db
from app.tasks.usage_sync import record_usage

router = APIRouter(tags=["Internal NoSQL Browse"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


@router.get("/projects/{project_id}/nosql/collections", dependencies=[InternalGuard])
async def list_collections(
    project_id: str,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    """List all non-reserved collection names in a project's MongoDB database."""
    db = get_project_db(mongo_database)
    try:
        names = await db.list_collection_names()
    except Exception as e:
        logger.error("Failed to list collections for %s: %s", mongo_database, e)
        return {"data": {"collections": []}}
    # Filter reserved collections
    public = sorted([n for n in names if not n.startswith("_")])
    return {"data": {"collections": public}}


@router.get(
    "/projects/{project_id}/nosql/collections/{collection}/documents",
    dependencies=[InternalGuard],
)
async def list_collection_documents(
    project_id: str,
    collection: str,
    mongo_database: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    skip: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Fetch documents from a collection — dashboard only."""
    from app.engines.nosql_engine import find_documents

    db = get_project_db(mongo_database)
    try:
        docs, total = await find_documents(
            db, collection, limit=limit, skip=skip, sort=[("_id", -1)]
        )
    except Exception as e:
        logger.error("Failed to list documents from %s: %s", collection, e)
        return {"data": {"docs": [], "total": 0}}
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": {"docs": docs, "total": total}}


@router.post(
    "/projects/{project_id}/nosql/collections/{collection}/documents",
    status_code=201,
    dependencies=[InternalGuard],
)
async def insert_document_internal(
    project_id: str,
    collection: str,
    mongo_database: str = Query(...),
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    from app.engines.nosql_engine import insert_document

    db = get_project_db(mongo_database)
    try:
        doc = await insert_document(db, collection, body)
    except Exception as e:
        logger.error("Failed to insert document into %s: %s", collection, e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"data": doc}


@router.delete(
    "/projects/{project_id}/nosql/collections/{collection}/documents/{doc_id}",
    dependencies=[InternalGuard],
)
async def delete_document_internal(
    project_id: str,
    collection: str,
    doc_id: str,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    from app.engines.nosql_engine import delete_document

    db = get_project_db(mongo_database)
    deleted = await delete_document(db, collection, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": {"deleted": True, "id": doc_id}}


@router.get("/projects/{project_id}/nosql/kv", dependencies=[InternalGuard])
async def list_kv_internal(
    project_id: str,
    mongo_database: str = Query(...),
    prefix: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    from app.engines.nosql_engine import kv_list

    db = get_project_db(mongo_database)
    entries = await kv_list(db, prefix=prefix, limit=limit)
    record_usage.delay(project_id, "nosql_reads", 1)
    return {"data": {"entries": entries}}


@router.put(
    "/projects/{project_id}/nosql/kv/{key:path}",
    status_code=201,
    dependencies=[InternalGuard],
)
async def set_kv_internal(
    project_id: str,
    key: str,
    mongo_database: str = Query(...),
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Set a KV entry — used by the dashboard instead of the SDK endpoint."""
    from app.engines.nosql_engine import kv_set

    value = body.get("value")
    ttl = body.get("ttl")
    db = get_project_db(mongo_database)
    await kv_set(db, key, value, ttl=ttl)
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": {"key": key, "value": value}}


@router.delete(
    "/projects/{project_id}/nosql/kv/{key:path}",
    dependencies=[InternalGuard],
)
async def delete_kv_internal(
    project_id: str,
    key: str,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    from app.engines.nosql_engine import kv_delete

    db = get_project_db(mongo_database)
    deleted = await kv_delete(db, key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    record_usage.delay(project_id, "nosql_writes", 1)
    return {"data": {"deleted": True, "key": key}}


class CreateCollectionInternalRequest(BaseModel):
    collection: str


@router.post(
    "/projects/{project_id}/nosql/collections",
    status_code=201,
    dependencies=[InternalGuard],
)
async def create_collection_internal(
    project_id: str,
    body: CreateCollectionInternalRequest,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    from app.provisioner.nosql_provisioner import create_collection

    try:
        await create_collection(mongo_database, body.collection)
    except Exception as e:
        logger.error("Failed to create collection %s: %s", body.collection, e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"data": {"collection": body.collection, "created": True}}


@router.delete(
    "/projects/{project_id}/nosql/collections/{collection}",
    dependencies=[InternalGuard],
)
async def drop_collection_internal(
    project_id: str,
    collection: str,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    from app.provisioner.nosql_provisioner import drop_collection

    try:
        await drop_collection(mongo_database, collection)
    except Exception as e:
        logger.error("Failed to drop collection %s: %s", collection, e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"data": {"collection": collection, "dropped": True}}