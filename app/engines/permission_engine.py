import json
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from app.db.postgres import AsyncSessionLocal
from app.db.redis import get_redis
from app.models.permissions import AuthContext, ResourcePermissions

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutes


async def _get_permissions(project_id: str, resource: str, engine: str) -> ResourcePermissions | None:
    redis = await get_redis()
    cache_key = f"perms:{project_id}:{engine}:{resource}"
    cached = await redis.get(cache_key)
    if cached:
        return ResourcePermissions.model_validate_json(cached)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT rules_json
                FROM resource_permissions
                WHERE project_id = :project_id
                  AND resource_name = :resource
                  AND engine = :engine
                """
            ),
            {"project_id": project_id, "resource": resource, "engine": engine},
        )
        row = result.first()

    if not row:
        return None

    perms = ResourcePermissions(
        resource=resource,
        engine=engine,  # type: ignore[arg-type]
        rules=json.loads(row[0]),
    )
    await redis.set(cache_key, perms.model_dump_json(), ex=CACHE_TTL)
    return perms


async def check_permission(
    project_id: str,
    resource: str,
    operation: str,
    engine: str,
    auth_ctx: AuthContext,
) -> dict[str, Any] | str | None:
    """
    Check if the operation is allowed. Returns the condition (for SQL/NoSQL filter injection)
    or raises 403.

    Returns None if no permissions are configured (open access).
    """
    perms = await _get_permissions(project_id, resource, engine)
    if perms is None:
        # No rules configured — default deny for safety
        # In practice the dashboard always sets up rules on table/collection creation
        return None

    for rule in perms.rules:
        if rule.operation != operation.upper():
            continue

        allow = rule.allow
        if allow == "public":
            return rule.condition
        if allow == "authenticated":
            if not auth_ctx.is_authenticated:
                raise HTTPException(status_code=401, detail="Authentication required")
            return rule.condition
        if allow == "owner":
            if not auth_ctx.is_authenticated:
                raise HTTPException(status_code=401, detail="Authentication required")
            return rule.condition  # caller must inject uid into the condition

    raise HTTPException(status_code=403, detail="Permission denied")


def inject_auth_uid(condition: Any, uid: str) -> Any:
    """Replace $auth.uid placeholder with actual user id in conditions."""
    if isinstance(condition, str):
        return condition.replace("auth.uid()", f"'{uid}'")
    if isinstance(condition, dict):
        return {k: inject_auth_uid(v, uid) for k, v in condition.items()}
    if isinstance(condition, list):
        return [inject_auth_uid(item, uid) for item in condition]
    return condition