# backend/app/api/internal/billing.py
import logging
import uuid
import hashlib
import json
import httpx
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db

router = APIRouter(prefix="/billing", tags=["Internal — Billing"])
logger = logging.getLogger(__name__)

PLAN_PRICES = {
    "free": {"ngn": 0, "usd": 0},
    "starter": {"ngn": 15_000, "usd": 10},
    "pro": {"ngn": 45_000, "usd": 30},
}


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


async def _get_or_create_organization(db: AsyncSession, user_id: str) -> dict[str, Any]:
    result = await db.execute(
        text(
            "SELECT id, plan, paystack_customer_id, stripe_customer_id, billing_currency, created_at "
            "FROM organizations WHERE owner_id = :user_id LIMIT 1"
        ),
        {"user_id": user_id},
    )
    org = result.mappings().first()
    if org:
        return dict(org)

    org_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            "INSERT INTO organizations (id, name, plan, owner_id, billing_currency, created_at) "
            "VALUES (:id, :name, 'free', :owner_id, 'NGN', :created_at)"
        ),
        {
            "id": org_id,
            "name": "Personal Organization",
            "owner_id": user_id,
            "created_at": now,
        },
    )
    await db.commit()
    logger.info("Created personal org %s for user %s", org_id, user_id)
    return {
        "id": org_id,
        "plan": "free",
        "paystack_customer_id": None,
        "stripe_customer_id": None,
        "billing_currency": "NGN",
        "created_at": now,
    }


def _format_invoice(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "period": row["period_start"].strftime("%b %Y") if row.get("period_start") else "",
        "amount_ngn": int(row["amount_ngn"]),
        "amount_usd": float(row["amount_usd"]),
        "status": row["status"],
        "payment_ref": row.get("payment_ref"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "period_start": row["period_start"].isoformat() if row.get("period_start") else None,
        "period_end": row["period_end"].isoformat() if row.get("period_end") else None,
    }


@router.get("/overview", dependencies=[Depends(require_internal)])
async def get_billing_overview(
    user_id: str = Query(..., description="Platform user ID"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    org = await _get_or_create_organization(db, user_id)
    plan = org.get("plan", "free")
    next_amount = PLAN_PRICES.get(plan, {"ngn": 0, "usd": 0})

    payment_method = "none"
    if org.get("paystack_customer_id") and settings.paystack_public_key:
        payment_method = "paystack"
    elif org.get("stripe_customer_id") and settings.stripe_public_key:
        payment_method = "stripe"

    latest_invoice = await db.execute(
        text(
            "SELECT period_end FROM invoices "
            "WHERE organization_id = :org_id "
            "ORDER BY period_end DESC LIMIT 1"
        ),
        {"org_id": org["id"]},
    )
    latest = latest_invoice.mappings().first()
    next_billing_date = latest["period_end"].isoformat() if latest and latest.get("period_end") else None

    return {
        "data": {
            "currentPlan": plan,
            "nextBillingDate": next_billing_date,
            "nextAmount_ngn": next_amount["ngn"],
            "nextAmount_usd": next_amount["usd"],
            "planSince": org["created_at"].isoformat() if org.get("created_at") else datetime.now(timezone.utc).isoformat(),
            "paymentMethod": payment_method,
            "cardLast4": None,
            "cardBrand": None,
        }
    }


@router.get("/invoices", dependencies=[Depends(require_internal)])
async def list_billing_invoices(
    user_id: str = Query(..., description="Platform user ID"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    org = await _get_or_create_organization(db, user_id)
    result = await db.execute(
        text(
            "SELECT id, amount_ngn, amount_usd, status, payment_ref, period_start, period_end, created_at "
            "FROM invoices "
            "WHERE organization_id = :org_id "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        ),
        {"org_id": org["id"], "limit": limit, "offset": offset},
    )
    invoices = [_format_invoice(dict(r)) for r in result.mappings()]
    return {"data": invoices}


@router.get("/usage", dependencies=[Depends(require_internal)])
async def get_billing_usage(
    user_id: str = Query(..., description="Platform user ID"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    org = await _get_or_create_organization(db, user_id)
    result = await db.execute(
        text(
            "SELECT ur.metric, SUM(ur.value) AS total "
            "FROM usage_records ur "
            "JOIN projects p ON p.id = ur.project_id "
            "WHERE p.organization_id = :org_id "
            "  AND ur.period_start >= NOW() - INTERVAL '30 days' "
            "GROUP BY ur.metric"
        ),
        {"org_id": org["id"]},
    )
    rows = {r["metric"]: int(r["total"]) for r in result.mappings()}
    return {
        "data": {
            "dbReads": rows.get("db_reads", 0),
            "dbWrites": rows.get("db_writes", 0),
            "nosqlReads": rows.get("nosql_reads", 0),
            "nosqlWrites": rows.get("nosql_writes", 0),
            "storageBytes": rows.get("storage_bytes", 0),
            "functionCalls": rows.get("function_calls", 0),
            "aiRequests": rows.get("ai_requests", 0),
        }
    }


@router.post("/checkout", dependencies=[Depends(require_internal)])
async def initiate_checkout(
    user_id: str = Query(..., description="Platform user ID"),
    invoice_id: str = Query(..., description="Invoice ID to pay for"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Initiate a Flutterwave payment for an invoice."""
    if not settings.flutterwave_secret_key:
        raise HTTPException(status_code=400, detail="Flutterwave is not configured")

    # Get invoice
    invoice_result = await db.execute(
        text(
            "SELECT i.id, i.amount_ngn, i.amount_usd, i.organization_id, i.status, "
            "       o.owner_id, u.email "
            "FROM invoices i "
            "JOIN organizations o ON o.id = i.organization_id "
            "JOIN users u ON u.id = o.owner_id "
            "WHERE i.id = :invoice_id"
        ),
        {"invoice_id": invoice_id},
    )
    invoice = invoice_result.mappings().first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to pay this invoice")

    if invoice["status"] == "paid":
        raise HTTPException(status_code=400, detail="Invoice already paid")

    # Create Flutterwave charge
    amount = invoice["amount_ngn"]  # Use NGN by default
    reference = f"inv_{invoice_id}_{uuid.uuid4().hex[:8]}"

    payload = {
        "tx_ref": reference,
        "amount": amount,
        "currency": "NGN",
        "customer": {
            "email": invoice["email"],
        },
        "customizations": {
            "title": "YourBaaS Invoice Payment",
            "description": f"Payment for invoice {invoice_id}",
            "logo": "https://yourbaas.com/logo.png",
        },
        "meta": {
            "invoice_id": invoice_id,
            "org_id": invoice["organization_id"],
        },
        "redirect_url": f"http://localhost:3000/u/{user_id}/billing?tab=invoices&payment_status=",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.flutterwave.com/v3/charges?type=card",
                json=payload,
                headers={"Authorization": f"Bearer {settings.flutterwave_secret_key}"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("status") == "success":
                logger.error("Flutterwave charge failed: %s", data.get("message"))
                raise HTTPException(status_code=400, detail="Failed to initiate payment")

            charge_data = data["data"]
            return {
                "data": {
                    "authorization_url": charge_data.get("processor_response"),
                    "access_code": charge_data.get("access_code"),
                    "reference": reference,
                    "amount": amount,
                    "currency": "NGN",
                }
            }
    except httpx.HTTPError as e:
        logger.error("Flutterwave API error: %s", str(e))
        raise HTTPException(status_code=500, detail="Payment gateway error")


@router.post("/webhook/flutterwave", dependencies=[Depends(require_internal)])
async def flutterwave_webhook(
    x_webhook_secret: str = Header(..., alias="x-webhook-secret"),
    body: dict[str, Any] = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Handle Flutterwave webhook callbacks."""
    if x_webhook_secret != settings.flutterwave_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # Verify webhook payload
    tx_ref = body.get("data", {}).get("tx_ref")
    if not tx_ref or not tx_ref.startswith("inv_"):
        return {"status": "ok"}

    # Extract invoice ID
    parts = tx_ref.split("_")
    if len(parts) < 2:
        return {"status": "ok"}

    invoice_id = parts[1]
    status = body.get("data", {}).get("status")

    if status == "successful":
        # Mark invoice as paid
        await db.execute(
            text("UPDATE invoices SET status = :status, payment_ref = :ref WHERE id = :id"),
            {"status": "paid", "ref": tx_ref, "id": invoice_id},
        )
        await db.commit()
        logger.info("Invoice %s marked as paid via Flutterwave", invoice_id)
    elif status == "failed":
        # Mark invoice as failed
        await db.execute(
            text("UPDATE invoices SET status = :status WHERE id = :id"),
            {"status": "failed", "id": invoice_id},
        )
        await db.commit()
        logger.warning("Invoice %s payment failed", invoice_id)

    return {"status": "ok"}
