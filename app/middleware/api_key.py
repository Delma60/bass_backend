import hashlib
import logging
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import text

from app.db.postgres import AsyncSessionLocal
from app.db.redis import get_redis
from app.config import settings

logger = logging.getLogger(__name__)

CACHE_TTL = settings.api_key_cache_ttl


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def _lookup_key_from_db(key_hash: str) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT ak.id, ak.project_id, ak.key_type, ak.is_active,
                       p.status as project_status, p.db_schema, p.mongo_database
                FROM api_keys ak
                JOIN projects p ON p.id = ak.project_id
                WHERE ak.key_hash = :key_hash
                """
            ),
            {"key_hash": key_hash},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def validate_api_key(request: Request) -> dict[str, Any]:
    """
    Validates Bearer token from Authorization header.
    Checks Redis cache first, falls back to PostgreSQL.
    Attaches project context to request.state.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    raw_key = auth_header.removeprefix("Bearer ").strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="Empty API key")

    key_hash = _hash_key(raw_key)
    cache_key = f"apikey:{key_hash}"

    redis = await get_redis()
    cached = await redis.get(cache_key)

    if cached:
        import json
        key_data = json.loads(cached)
    else:
        key_data = await _lookup_key_from_db(key_hash)
        if key_data:
            import json
            await redis.set(cache_key, json.dumps(key_data), ex=CACHE_TTL)

    if not key_data:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not key_data.get("is_active"):
        raise HTTPException(status_code=401, detail="API key is inactive")

    if key_data.get("project_status") != "active":
        raise HTTPException(status_code=403, detail="Project is not active")

    # Attach to request state
    request.state.project_id = key_data["project_id"]
    request.state.db_schema = key_data["db_schema"]
    request.state.mongo_database = key_data["mongo_database"]
    request.state.key_type = key_data["key_type"]

    return key_data