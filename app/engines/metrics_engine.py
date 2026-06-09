# backend/app/engines/metrics_engine.py
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def get_platform_metrics(db: AsyncSession) -> dict[str, Any]:
    """Return platform-wide aggregate metrics."""
    result = await db.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM users)                                       AS total_users,
          (SELECT COUNT(*) FROM organizations)                               AS total_organizations,
          (SELECT COUNT(*) FROM projects)                                    AS total_projects,
          (SELECT COUNT(*) FROM projects WHERE status = 'active')            AS active_projects,
          (SELECT COALESCE(SUM(amount_ngn), 0) FROM invoices
             WHERE created_at >= NOW() - INTERVAL '30 days')                 AS monthly_revenue_ngn,
          (SELECT COALESCE(SUM(amount_usd), 0) FROM invoices
             WHERE created_at >= NOW() - INTERVAL '30 days')                 AS monthly_revenue_usd
    """))
    row = result.mappings().first()
    return dict(row) if row else {}