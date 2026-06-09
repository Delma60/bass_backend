# backend/app/api/internal/notifications.py
"""
Internal-only endpoints for the dashboard notification system.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.

Endpoints:
  GET    /notifications?user_id=&limit=&unread_only=  — list notifications
  POST   /notifications                               — create a notification
  PATCH  /notifications/{id}/read                    — mark one as read
  POST   /notifications/read-all?user_id=            — mark all as read
  DELETE /notifications/{id}                         — delete one
  GET    /notifications/unread-count?user_id=        — fast count badge
"""
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(tags=["Internal Notifications"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


def _serialize(row: dict) -> dict:
    r = dict(row)
    if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
        r["created_at"] = r["created_at"].isoformat()
    return r


# ─── List ──────────────────────────────────────────────────────────────────────

@router.get("/notifications", dependencies=[InternalGuard])
async def list_notifications(
    user_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return notifications for a user, newest first."""
    conditions = ["user_id = :user_id"]
    params: dict[str, Any] = {"user_id": user_id, "limit": limit, "offset": offset}

    if unread_only:
        conditions.append("is_read = false")

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT id, user_id, type, title, body, href, meta, is_read, created_at
            FROM notifications
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    notifications = [_serialize(dict(r._mapping)) for r in result]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM notifications WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": notifications, "meta": {"count": total, "limit": limit, "offset": offset}}


# ─── Unread count (badge) ──────────────────────────────────────────────────────

@router.get("/notifications/unread-count", dependencies=[InternalGuard])
async def unread_count(
    user_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cheap query — just the unread badge number."""
    result = await db.execute(
        text("SELECT COUNT(*) FROM notifications WHERE user_id = :uid AND is_read = false"),
        {"uid": user_id},
    )
    count = result.scalar() or 0
    return {"data": {"count": count}}


# ─── Create ────────────────────────────────────────────────────────────────────

class CreateNotificationRequest(BaseModel):
    user_id: str
    type: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str | None = None
    href: str | None = None
    meta: dict[str, Any] | None = None


@router.post("/notifications", status_code=201, dependencies=[InternalGuard])
async def create_notification(
    body: CreateNotificationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a notification for a user."""
    notif_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO notifications (id, user_id, type, title, body, href, meta)
            VALUES (:id, :user_id, :type, :title, :body, :href, :meta)
        """),
        {
            "id": notif_id,
            "user_id": body.user_id,
            "type": body.type,
            "title": body.title,
            "body": body.body,
            "href": body.href,
            "meta": json.dumps(body.meta) if body.meta else None,
        },
    )
    await db.commit()
    return {"data": {"id": notif_id, "created": True}}


# ─── Mark one as read ──────────────────────────────────────────────────────────

@router.patch("/notifications/{notif_id}/read", dependencies=[InternalGuard])
async def mark_read(
    notif_id: str,
    user_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            UPDATE notifications SET is_read = true
            WHERE id = :id AND user_id = :user_id
            RETURNING id
        """),
        {"id": notif_id, "user_id": user_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.commit()
    return {"data": {"id": notif_id, "read": True}}


# ─── Mark all as read ──────────────────────────────────────────────────────────

@router.post("/notifications/read-all", dependencies=[InternalGuard])
async def mark_all_read(
    user_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("UPDATE notifications SET is_read = true WHERE user_id = :uid AND is_read = false"),
        {"uid": user_id},
    )
    await db.commit()
    return {"data": {"marked": result.rowcount}}


# ─── Delete one ────────────────────────────────────────────────────────────────

@router.delete("/notifications/{notif_id}", dependencies=[InternalGuard])
async def delete_notification(
    notif_id: str,
    user_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("DELETE FROM notifications WHERE id = :id AND user_id = :uid RETURNING id"),
        {"id": notif_id, "uid": user_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.commit()
    return {"data": {"id": notif_id, "deleted": True}}