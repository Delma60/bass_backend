# backend/app/api/admin_api/middleware.py
"""
Middleware / FastAPI dependencies for the /admin-api/* router.

Every route in this package must depend on `require_admin_panel`.

Two-factor check on every request:
  1. Authorization: Bearer <service_key>   — validated against api_keys table
  2. X-Admin-Integration-Secret: <secret>  — validated against settings

This keeps the admin integration completely decoupled from:
  - /v1/*         (public SDK routes, validated by api_key middleware)
  - /internal/*   (Next.js dashboard proxy, validated by X-Internal-Secret)
  - /superadmin/* (BaaS staff panel, validated by staff JWT)
"""
import hashlib
import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdminContext:
    """Carries the verified identity of the calling admin panel."""
    project_id: str
    key_id: str
    label: str


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def require_admin_panel(
    authorization: str = Header(...),
    x_admin_integration_secret: str = Header(..., alias="x-admin-integration-secret"),
    db: AsyncSession = Depends(get_db),
) -> AdminContext:
    """
    FastAPI dependency — validates the two-factor admin panel identity.
    Use as:  Depends(require_admin_panel)

    Raises 401 for any auth failure. Never reveals which check failed.
    """
    # 1. Validate shared secret first — cheapest check, no DB hit
    if x_admin_integration_secret != settings.admin_integration_secret:
        logger.warning("Admin API: invalid integration secret")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 2. Validate service-role Bearer token
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    raw_key = authorization.removeprefix("Bearer ").strip()
    if not raw_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    key_hash = _hash_key(raw_key)

    result = await db.execute(
        text("""
            SELECT ak.id, ak.project_id, ak.is_active, ak.key_type, ak.label,
                   p.status AS project_status
            FROM api_keys ak
            JOIN projects p ON p.id = ak.project_id
            WHERE ak.key_hash = :key_hash
        """),
        {"key_hash": key_hash},
    )
    row = result.mappings().first()

    if not row:
        logger.warning("Admin API: unknown service key (hash prefix %s…)", key_hash[:8])
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not row["is_active"]:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Only service-role keys may call admin-api
    if row["key_type"] != "service":
        raise HTTPException(
            status_code=403,
            detail="Admin API requires a service-role API key",
        )

    if row["project_status"] != "active":
        raise HTTPException(status_code=403, detail="Project is not active")

    return AdminContext(
        project_id=row["project_id"],
        key_id=row["id"],
        label=row["label"] or "service key",
    )