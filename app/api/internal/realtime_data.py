# backend/app/api/internal/realtime_data.py
"""
Internal-only endpoints for browsing and mutating realtime database data.
The realtime DB stores its tree in a MongoDB collection called `_rtdb`
where each document is { path: "/users/abc", value: {...} }.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.db.mongo import get_project_db
from app.tasks.usage_sync import record_usage

router = APIRouter(tags=["Internal Realtime Data"])
logger = logging.getLogger(__name__)

RTDB_COLLECTION = "_rtdb"


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


def _path_to_key(path: str) -> str:
    """Normalise a path to a dot-notation key for MongoDB."""
    return path.strip("/").replace("/", ".")


@router.get("/projects/{project_id}/rtdb/data", dependencies=[InternalGuard])
async def get_rtdb_data(
    project_id: str,
    mongo_database: str = Query(...),
    path: str = Query(default="/"),
) -> dict[str, Any]:
    """
    Return the realtime database tree rooted at `path`.
    Builds a nested dict from all stored paths.
    """
    db = get_project_db(mongo_database)
    coll = db[RTDB_COLLECTION]

    norm_path = path.strip("/")

    try:
        # Fetch all docs that start with this path
        if norm_path:
            cursor = coll.find({"path": {"$regex": f"^/{norm_path}"}})
        else:
            cursor = coll.find({})

        docs = []
        async for doc in cursor:
            docs.append({"path": doc["path"], "value": doc.get("value"), "type": doc.get("type", "auto")})

        # Build tree
        tree: dict[str, Any] = {}
        for doc in docs:
            parts = [p for p in doc["path"].strip("/").split("/") if p]
            node = tree
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    node[part] = {"__value__": doc["value"], "__type__": doc.get("type", "auto")}
                else:
                    node = node.setdefault(part, {})

        record_usage.delay(project_id, "nosql_reads", 1)
        return {"data": {"tree": tree, "path": path, "count": len(docs)}}
    except Exception as e:
        logger.error("Failed to get rtdb data: %s", e)
        return {"data": {"tree": {}, "path": path, "count": 0}}


class SetRtdbValueRequest(BaseModel):
    path: str
    value: Any
    type: str = "auto"  # auto | string | number | boolean | object


@router.put("/projects/{project_id}/rtdb/data", dependencies=[InternalGuard])
async def set_rtdb_value(
    project_id: str,
    mongo_database: str = Query(...),
    body: SetRtdbValueRequest = Body(...),
) -> dict[str, Any]:
    """Set a value at a given path (upsert)."""
    db = get_project_db(mongo_database)
    coll = db[RTDB_COLLECTION]
    norm_path = "/" + body.path.strip("/")

    # Cast value based on type
    value = body.value
    if body.type == "number":
        try:
            value = float(body.value) if "." in str(body.value) else int(body.value)
        except (ValueError, TypeError):
            pass
    elif body.type == "boolean":
        if isinstance(value, str):
            value = value.lower() == "true"
    elif body.type == "object":
        if isinstance(value, str):
            import json
            try:
                value = json.loads(value)
            except Exception:
                pass

    try:
        await coll.update_one(
            {"path": norm_path},
            {"$set": {"path": norm_path, "value": value, "type": body.type}},
            upsert=True,
        )
        # Broadcast via Redis
        try:
            from app.engines.realtime_engine import broadcast_event
            await broadcast_event(project_id, "rtdb", "SET", {"path": norm_path, "value": value})
        except Exception:
            pass
        record_usage.delay(project_id, "nosql_writes", 1)
        return {"data": {"path": norm_path, "value": value, "set": True}}
    except Exception as e:
        logger.error("Failed to set rtdb value: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/projects/{project_id}/rtdb/data", dependencies=[InternalGuard])
async def delete_rtdb_value(
    project_id: str,
    mongo_database: str = Query(...),
    path: str = Query(...),
) -> dict[str, Any]:
    """Delete a value (and all children) at a given path."""
    db = get_project_db(mongo_database)
    coll = db[RTDB_COLLECTION]
    norm_path = "/" + path.strip("/")

    try:
        # Delete exact + all children
        result = await coll.delete_many(
            {"path": {"$regex": f"^{norm_path}(/|$)"}}
        )
        try:
            from app.engines.realtime_engine import broadcast_event
            await broadcast_event(project_id, "rtdb", "DELETE", {"path": norm_path})
        except Exception:
            pass
        record_usage.delay(project_id, "nosql_writes", 1)
        return {"data": {"path": norm_path, "deleted": result.deleted_count}}
    except Exception as e:
        logger.error("Failed to delete rtdb value: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}/rtdb/stats", dependencies=[InternalGuard])
async def get_rtdb_stats(
    project_id: str,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    """Return stats for the realtime database."""
    db = get_project_db(mongo_database)
    coll = db[RTDB_COLLECTION]
    try:
        count = await coll.count_documents({})
        record_usage.delay(project_id, "nosql_reads", 1)
        return {"data": {"total_nodes": count, "project_id": project_id}}
    except Exception:
        return {"data": {"total_nodes": 0, "project_id": project_id}}