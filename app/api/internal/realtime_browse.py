# backend/app/api/internal/realtime_browse.py
"""
Internal-only endpoints for the dashboard to manage realtime channels.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.
"""
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(tags=["Internal Realtime"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


# ─── Channels ────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/realtime/channels", dependencies=[InternalGuard])
async def list_channels(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all realtime channels configured for a project."""
    try:
        result = await db.execute(
            text("""
                SELECT id, name, path, access_rule, is_active,
                       enable_presence, created_at
                FROM realtime_channels
                WHERE project_id = :project_id
                ORDER BY created_at DESC
            """),
            {"project_id": project_id},
        )
        channels = []
        for row in result.mappings():
            ch = dict(row)
            if ch.get("created_at") and hasattr(ch["created_at"], "isoformat"):
                ch["created_at"] = ch["created_at"].isoformat()
            channels.append(ch)
        return {"data": {"channels": channels}}
    except Exception as e:
        logger.warning("realtime_channels table may not exist: %s", e)
        return {"data": {"channels": []}}


class CreateChannelRequest(BaseModel):
    name: str
    path: str
    access_rule: str = "auth != null"
    enable_presence: bool = True


@router.post("/projects/{project_id}/realtime/channels", status_code=201, dependencies=[InternalGuard])
async def create_channel(
    project_id: str,
    body: CreateChannelRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new realtime channel for a project."""
    channel_id = str(uuid.uuid4())
    try:
        await db.execute(
            text("""
                INSERT INTO realtime_channels
                    (id, project_id, name, path, access_rule, is_active, enable_presence)
                VALUES
                    (:id, :project_id, :name, :path, :access_rule, true, :enable_presence)
            """),
            {
                "id": channel_id,
                "project_id": project_id,
                "name": body.name,
                "path": body.path if body.path.startswith("/") else f"/{body.path}",
                "access_rule": body.access_rule,
                "enable_presence": body.enable_presence,
            },
        )
        await db.commit()
        return {"data": {"id": channel_id, "name": body.name, "created": True}}
    except Exception as e:
        logger.error("Failed to create channel: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/projects/{project_id}/realtime/channels/{channel_id}", dependencies=[InternalGuard])
async def delete_channel(
    project_id: str,
    channel_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            DELETE FROM realtime_channels
            WHERE id = :channel_id AND project_id = :project_id
            RETURNING id
        """),
        {"channel_id": channel_id, "project_id": project_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Channel not found")
    await db.commit()
    return {"data": {"id": channel_id, "deleted": True}}


# ─── Security Rules ───────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/realtime/rules", dependencies=[InternalGuard])
async def get_rules(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get the realtime security rules JSON for a project."""
    try:
        result = await db.execute(
            text("""
                SELECT rules_json, updated_at
                FROM realtime_rules
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        row = result.mappings().first()
        if not row:
            default_rules = """{
  "rules": {
    ".read": "auth != null",
    ".write": "auth != null"
  }
}"""
            return {"data": {"rules_json": default_rules, "updated_at": None}}
        r = dict(row)
        if r.get("updated_at") and hasattr(r["updated_at"], "isoformat"):
            r["updated_at"] = r["updated_at"].isoformat()
        return {"data": r}
    except Exception as e:
        logger.warning("realtime_rules table may not exist: %s", e)
        default_rules = """{
  "rules": {
    ".read": "auth != null",
    ".write": "auth != null"
  }
}"""
        return {"data": {"rules_json": default_rules, "updated_at": None}}


class UpdateRulesRequest(BaseModel):
    rules_json: str


@router.put("/projects/{project_id}/realtime/rules", dependencies=[InternalGuard])
async def update_rules(
    project_id: str,
    body: UpdateRulesRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upsert realtime security rules for a project."""
    try:
        await db.execute(
            text("""
                INSERT INTO realtime_rules (id, project_id, rules_json, updated_at)
                VALUES (:id, :project_id, :rules_json, NOW())
                ON CONFLICT (project_id) DO UPDATE
                  SET rules_json = EXCLUDED.rules_json,
                      updated_at = NOW()
            """),
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "rules_json": body.rules_json,
            },
        )
        await db.commit()
        return {"data": {"saved": True}}
    except Exception as e:
        logger.error("Failed to save realtime rules: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ─── Stats ────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/realtime/stats", dependencies=[InternalGuard])
async def get_realtime_stats(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return aggregate realtime stats for the dashboard."""
    try:
        result = await db.execute(
            text("""
                SELECT
                    COUNT(*)                                         AS total_channels,
                    COUNT(*) FILTER (WHERE is_active = true)        AS active_channels,
                    COUNT(*) FILTER (WHERE enable_presence = true)  AS presence_channels
                FROM realtime_channels
                WHERE project_id = :project_id
            """),
            {"project_id": project_id},
        )
        row = result.mappings().first()
        stats = dict(row) if row else {"total_channels": 0, "active_channels": 0, "presence_channels": 0}
        # connected clients would come from Redis/Socket.io in prod
        stats["connected_clients"] = 0
        return {"data": stats}
    except Exception:
        return {"data": {
            "total_channels": 0,
            "active_channels": 0,
            "presence_channels": 0,
            "connected_clients": 0,
        }}