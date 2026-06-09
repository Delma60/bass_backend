# backend/app/api/superadmin/metrics.py
import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.engines.metrics_engine import get_platform_metrics
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.staff import StaffContext

router = APIRouter(prefix="/metrics")
logger = logging.getLogger(__name__)


@router.get("")
async def platform_metrics(
    staff: StaffContext = Depends(require_staff_role(StaffRole.ops)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Platform-wide aggregate metrics."""
    data = await get_platform_metrics(db)
    return {"data": data}