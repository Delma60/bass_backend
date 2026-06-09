# backend/app/api/superadmin/billing.py
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db
from app.middleware.staff_auth import StaffRole, require_staff_role
from app.models.staff import StaffContext
from app.api.superadmin._audit import write_audit_log

router = APIRouter(prefix="/billing")
logger = logging.getLogger(__name__)


@router.get("")
async def list_invoices(
    staff: StaffContext = Depends(require_staff_role(StaffRole.billing)),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
) -> dict[str, Any]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if status:
        conditions.append("i.status = :status")
        params["status"] = status
    if org_id:
        conditions.append("i.organization_id = :org_id")
        params["org_id"] = org_id

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT i.id, i.organization_id, i.amount_ngn, i.amount_usd,
                   i.status, i.payment_ref, i.period_start, i.period_end,
                   i.created_at, o.name AS org_name, o.plan AS org_plan
            FROM invoices i
            LEFT JOIN organizations o ON o.id = i.organization_id
            WHERE {where}
            ORDER BY i.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    invoices = [dict(r) for r in result.mappings()]

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM invoices i WHERE {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    )
    total = count_result.scalar() or 0

    return {"data": invoices, "meta": {"count": total, "limit": limit, "offset": offset}}


@router.get("/summary")
async def billing_summary(
    staff: StaffContext = Depends(require_staff_role(StaffRole.billing)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Revenue summary for the current and previous month."""
    result = await db.execute(text("""
        SELECT
            COALESCE(SUM(amount_ngn) FILTER (
                WHERE created_at >= date_trunc('month', NOW())
            ), 0) AS current_month_ngn,
            COALESCE(SUM(amount_usd) FILTER (
                WHERE created_at >= date_trunc('month', NOW())
            ), 0) AS current_month_usd,
            COALESCE(SUM(amount_ngn) FILTER (
                WHERE created_at >= date_trunc('month', NOW()) - INTERVAL '1 month'
                  AND created_at  < date_trunc('month', NOW())
            ), 0) AS prev_month_ngn,
            COALESCE(SUM(amount_usd) FILTER (
                WHERE created_at >= date_trunc('month', NOW()) - INTERVAL '1 month'
                  AND created_at  < date_trunc('month', NOW())
            ), 0) AS prev_month_usd,
            COUNT(*) FILTER (WHERE status = 'paid')    AS paid_count,
            COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
            COUNT(*) FILTER (WHERE status = 'failed')  AS failed_count
        FROM invoices
        WHERE created_at >= NOW() - INTERVAL '2 months'
    """))
    row = result.mappings().first()
    return {"data": dict(row) if row else {}}


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.billing)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT i.id, i.organization_id, i.amount_ngn, i.amount_usd,
                   i.status, i.payment_ref, i.period_start, i.period_end,
                   i.created_at, o.name AS org_name, o.plan AS org_plan,
                   u.email AS owner_email
            FROM invoices i
            LEFT JOIN organizations o ON o.id = i.organization_id
            LEFT JOIN users u ON u.id = o.owner_id
            WHERE i.id = :invoice_id
        """),
        {"invoice_id": invoice_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {"data": dict(row)}


@router.post("/{invoice_id}/retry")
async def retry_invoice_payment(
    invoice_id: str,
    staff: StaffContext = Depends(require_staff_role(StaffRole.billing)),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Manually trigger a payment retry for a failed invoice."""
    result = await db.execute(
        text("SELECT id, organization_id, status FROM invoices WHERE id = :invoice_id"),
        {"invoice_id": invoice_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if row["status"] == "paid":
        raise HTTPException(status_code=400, detail="Invoice is already paid")

    # Re-queue via Celery
    from app.tasks.invoice_gen import charge_invoice

    org_result = await db.execute(
        text("SELECT plan FROM organizations WHERE id = :org_id"),
        {"org_id": row["organization_id"]},
    )
    org = org_result.mappings().first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    charge_invoice.delay(invoice_id, row["organization_id"], org["plan"])

    await write_audit_log(db, staff, "billing.retry", invoice_id)
    return {"data": {"invoice_id": invoice_id, "queued": True}}