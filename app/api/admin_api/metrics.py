# backend/app/api/admin_api/metrics.py
"""
/admin-api/metrics — platform-wide aggregate metrics for the admin panel.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_api.middleware import AdminContext, require_admin_panel
from app.db.postgres import get_db
from app.engines.metrics_engine import get_platform_metrics

router = APIRouter(prefix="/metrics", tags=["Admin API — Metrics"])
logger = logging.getLogger(__name__)


@router.get("")
async def platform_metrics(
    ctx: AdminContext = Depends(require_admin_panel),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Platform-wide aggregate metrics. Read-only."""
    data = await get_platform_metrics(db)
    return {"data": data}