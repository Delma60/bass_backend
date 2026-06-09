# backend/app/api/internal/settings_browse.py
"""
Internal-only endpoints for the project settings dashboard.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.

Endpoints:
  GET    /projects/{id}/api-keys             — list all API keys for a project
  POST   /projects/{id}/api-keys             — create a new API key
  DELETE /projects/{id}/api-keys/{kid}       — revoke an API key
  GET    /projects/{id}/settings             — get editable project settings
  PATCH  /projects/{id}/settings             — update project name / description / status
  GET    /projects/{id}/members              — list org members who have project access
  POST   /projects/{id}/members              — invite a member (placeholder)
  DELETE /projects/{id}/members/{mid}        — remove a member (placeholder)
"""
import hashlib
import logging
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(tags=["Internal Settings"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


# ─── API Keys ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/api-keys", dependencies=[InternalGuard])
async def list_api_keys(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all API keys for a project (key hashes only — never raw keys)."""
    try:
        result = await db.execute(
            text("""
                SELECT id, project_id, key_type, label, is_active, created_at,
                       LEFT(key_hash, 8) AS key_prefix
                FROM api_keys
                WHERE project_id = :project_id
                ORDER BY created_at ASC
            """),
            {"project_id": project_id},
        )
        keys = []
        for row in result.mappings():
            r = dict(row)
            if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                r["created_at"] = r["created_at"].isoformat()
            keys.append(r)
        return {"data": {"keys": keys}}
    except Exception as e:
        logger.warning("Failed to list API keys for %s: %s", project_id, e)
        return {"data": {"keys": []}}


class CreateApiKeyRequest(BaseModel):
    key_type: str = Field(default="anon", pattern="^(anon|service)$")
    label: str | None = None


@router.post("/projects/{project_id}/api-keys", status_code=201, dependencies=[InternalGuard])
async def create_project_api_key(
    project_id: str,
    body: CreateApiKeyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new API key for a project. Returns the raw key ONCE."""
    exists = await db.execute(
        text("SELECT id FROM projects WHERE id = :pid"), {"pid": project_id}
    )
    if not exists.first():
        raise HTTPException(status_code=404, detail="Project not found")

    raw_key = f"sk_{body.key_type}_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_id = str(uuid.uuid4())
    label = body.label or f"{body.key_type} key"

    await db.execute(
        text("""
            INSERT INTO api_keys (id, project_id, key_hash, key_type, is_active, label)
            VALUES (:id, :project_id, :key_hash, :key_type, true, :label)
        """),
        {
            "id": key_id,
            "project_id": project_id,
            "key_hash": key_hash,
            "key_type": body.key_type,
            "label": label,
        },
    )
    await db.commit()

    return {
        "data": {
            "id": key_id,
            "key": raw_key,
            "key_type": body.key_type,
            "label": label,
            "is_active": True,
        }
    }


@router.delete("/projects/{project_id}/api-keys/{key_id}", dependencies=[InternalGuard])
async def revoke_project_api_key(
    project_id: str,
    key_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Revoke (soft-delete) an API key."""
    result = await db.execute(
        text("""
            UPDATE api_keys SET is_active = false
            WHERE id = :key_id AND project_id = :project_id
            RETURNING id
        """),
        {"key_id": key_id, "project_id": project_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="API key not found")
    await db.commit()

    try:
        from app.db.redis import get_redis
        redis = await get_redis()
        async for cache_key in redis.scan_iter("apikey:*"):
            cached = await redis.get(cache_key)
            if cached:
                import json
                data = json.loads(cached)
                if data.get("id") == key_id:
                    await redis.delete(cache_key)
                    break
    except Exception:
        pass

    return {"data": {"id": key_id, "revoked": True}}


# ─── Project Settings ─────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/settings", dependencies=[InternalGuard])
async def get_project_settings(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get the editable settings for a project."""
    result = await db.execute(
        text("""
            SELECT p.id, p.name, p.slug, p.region, p.status,
                   p.db_schema, p.mongo_database, p.created_at,
                   o.id AS org_id, o.name AS org_name, o.plan AS org_plan,
                   o.owner_id
            FROM projects p
            LEFT JOIN organizations o ON o.id = p.organization_id
            WHERE p.id = :project_id
        """),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    r = dict(row)
    if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
        r["created_at"] = r["created_at"].isoformat()
    return {"data": r}


class UpdateProjectSettingsRequest(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=40)
    description: str | None = None
    status: str | None = Field(None, pattern="^(active|paused)$")
    environment_type: str | None = None  # stored as metadata — ignored for now


@router.patch("/projects/{project_id}/settings", dependencies=[InternalGuard])
async def update_project_settings(
    project_id: str,
    body: UpdateProjectSettingsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Update editable project fields (name, description, status)."""
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.status is not None:
        updates["status"] = body.status

    if not updates:
        # Nothing to update — return current settings
        return await get_project_settings(project_id, db)

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["project_id"] = project_id

    result = await db.execute(
        text(f"UPDATE projects SET {set_clause} WHERE id = :project_id RETURNING id, name, status"),
        updates,
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.commit()

    # If status changed to paused, bust all API key caches for this project
    if body.status:
        try:
            from app.db.redis import get_redis
            import json
            redis = await get_redis()
            async for cache_key in redis.scan_iter("apikey:*"):
                cached = await redis.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    if data.get("project_id") == project_id:
                        await redis.delete(cache_key)
        except Exception:
            pass

    return {"data": dict(row)}


# ─── Members ──────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/members", dependencies=[InternalGuard])
async def list_project_members(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    List members of the organization that owns this project.
    All org members have access to all org projects.
    """
    try:
        # Owner
        result = await db.execute(
            text("""
                SELECT u.id, u.email, u.name, u.created_at, 'owner' AS role
                FROM projects p
                JOIN organizations o ON o.id = p.organization_id
                JOIN users u ON u.id = o.owner_id
                WHERE p.id = :project_id
            """),
            {"project_id": project_id},
        )
        owner_rows = [dict(r._mapping) for r in result]

        # Other org members
        result2 = await db.execute(
            text("""
                SELECT u.id, u.email, u.name, u.created_at, 'member' AS role
                FROM projects p
                JOIN organizations o ON o.id = p.organization_id
                JOIN organization_members om ON om.organization_id = o.id
                JOIN users u ON u.id = om.user_id
                WHERE p.id = :project_id
                  AND u.id != (SELECT owner_id FROM organizations WHERE id = o.id LIMIT 1)
                ORDER BY u.created_at
            """),
            {"project_id": project_id},
        )
        member_rows = [dict(r._mapping) for r in result2]

        all_members = owner_rows + member_rows

        # Serialize datetime
        for m in all_members:
            if m.get("created_at") and hasattr(m["created_at"], "isoformat"):
                m["created_at"] = m["created_at"].isoformat()

        return {"data": {"members": all_members}}
    except Exception as e:
        logger.warning("Failed to list members for %s: %s", project_id, e)
        # Minimal fallback — just the owner
        try:
            result = await db.execute(
                text("""
                    SELECT u.id, u.email, u.name, u.created_at, 'owner' AS role
                    FROM projects p
                    JOIN organizations o ON o.id = p.organization_id
                    JOIN users u ON u.id = o.owner_id
                    WHERE p.id = :project_id
                """),
                {"project_id": project_id},
            )
            members = []
            for row in result.mappings():
                r = dict(row)
                if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                    r["created_at"] = r["created_at"].isoformat()
                members.append(r)
            return {"data": {"members": members}}
        except Exception:
            return {"data": {"members": []}}


class InviteMemberRequest(BaseModel):
    email: str


@router.post("/projects/{project_id}/members", status_code=201, dependencies=[InternalGuard])
async def invite_project_member(
    project_id: str,
    body: InviteMemberRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Invite a user to the organization that owns this project.
    If the user exists, adds them to organization_members.
    Otherwise queues an invite (future: email invite system).
    """
    # Check if user exists
    result = await db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": body.email},
    )
    user_row = result.first()

    if user_row:
        user_id = user_row[0]
        # Get org ID for this project
        org_result = await db.execute(
            text("SELECT organization_id FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        )
        org_row = org_result.first()
        if org_row:
            org_id = org_row[0]
            # Add to organization_members if not already
            try:
                await db.execute(
                    text("""
                        INSERT INTO organization_members (id, organization_id, user_id, role)
                        VALUES (:id, :org_id, :user_id, 'member')
                        ON CONFLICT (organization_id, user_id) DO NOTHING
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "org_id": org_id,
                        "user_id": user_id,
                    },
                )
                await db.commit()
            except Exception as e:
                logger.warning("Failed to add org member: %s", e)

    return {"data": {"invited": True, "email": body.email}}


@router.delete("/projects/{project_id}/members/{member_id}", dependencies=[InternalGuard])
async def remove_project_member(
    project_id: str,
    member_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Remove a member from the organization (removes project access)."""
    # Get org ID
    
    result = await db.execute(
        text("SELECT organization_id FROM projects WHERE id = :project_id"),
        {"project_id": project_id},
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    org_id = row[0]
    await db.execute(
        text("""
            DELETE FROM organization_members
            WHERE organization_id = :org_id AND user_id = :user_id
        """),
        {"org_id": org_id, "user_id": member_id},
    )
    await db.commit()
    return {"data": {"removed": True, "id": member_id}}