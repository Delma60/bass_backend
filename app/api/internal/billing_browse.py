# backend/app/api/internal/billing_browse.py
"""
Internal-only billing endpoints for the dashboard.
NOT exposed via /v1/ — only callable from Next.js with X-Internal-Secret.

All routes take `project_id` as path parameter. Internally we resolve the
organization that owns that project to fetch/update subscription & invoice data.

Endpoints:
  GET    /billing/plans                            — plan limits catalogue (no project needed)
  GET    /billing/{project_id}/overview            — plan, usage, invoice history
  GET    /billing/{project_id}/subscription        — current subscription detail
  POST   /billing/{project_id}/checkout/initiate   — start Flutterwave hosted checkout
  POST   /billing/{project_id}/checkout/verify     — verify a completed Flutterwave tx
  POST   /billing/{project_id}/cancel              — cancel at period end
  GET    /billing/usage/{project_id}               — per-project usage (30d rolling)
  POST   /webhooks/flutterwave                     — Flutterwave webhook (internal route)
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.postgres import get_db
from app.db.redis import get_redis

router = APIRouter(tags=["Internal Billing"])
logger = logging.getLogger(__name__)


# ─── Auth guard ───────────────────────────────────────────────────────────────

async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _serialize(row: dict) -> dict:
    r = dict(row)
    for f in ("created_at", "updated_at", "current_period_start", "current_period_end", "refreshed_at"):
        if r.get(f) and hasattr(r[f], "isoformat"):
            r[f] = r[f].isoformat()
    return r


async def _get_org_id_from_project(db: AsyncSession, project_id: str) -> str:
    """Resolve the organization_id that owns a given project."""
    result = await db.execute(
        text("SELECT organization_id FROM projects WHERE id = :project_id"),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return str(row["organization_id"])


async def _get_or_create_subscription(db: AsyncSession, org_id: str) -> dict:
    result = await db.execute(
        text("SELECT * FROM subscriptions WHERE organization_id = :org_id"),
        {"org_id": org_id},
    )
    row = result.mappings().first()
    if row:
        return _serialize(dict(row))

    sub_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO subscriptions (id, organization_id, plan, status, currency, cancel_at_period_end)
            VALUES (:id, :org_id, 'free', 'active', 'NGN', false)
            ON CONFLICT (organization_id) DO NOTHING
        """),
        {"id": sub_id, "org_id": org_id},
    )
    await db.commit()
    result2 = await db.execute(
        text("SELECT * FROM subscriptions WHERE organization_id = :org_id"),
        {"org_id": org_id},
    )
    row2 = result2.mappings().first()
    return _serialize(dict(row2)) if row2 else {
        "plan": "free",
        "status": "active",
        "cancel_at_period_end": False,
        "amount_ngn": 0,
        "amount_usd": 0,
        "currency": "NGN",
        "current_period_start": None,
        "current_period_end": None,
    }


TRACKED_METRICS = [
    "db_reads",
    "db_writes",
    "nosql_reads",
    "nosql_writes",
    "storage_bytes",
    "function_calls",
    "ai_requests",
]


async def _get_live_org_usage(org_id: str) -> dict[str, int]:
    redis = await get_redis()
    from app.db.postgres import AsyncSessionLocal

    project_ids = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id FROM projects WHERE organization_id = :org_id"),
            {"org_id": org_id},
        )
        project_ids = [row[0] for row in result.all()]

    totals = {metric: 0 for metric in TRACKED_METRICS}
    if not project_ids:
        return totals

    pipe = redis.pipeline()
    for project_id in project_ids:
        for metric in TRACKED_METRICS:
            pipe.get(f"usage:{project_id}:{metric}")
    values = await pipe.execute()

    for idx, raw in enumerate(values):
        if raw:
            metric = TRACKED_METRICS[idx % len(TRACKED_METRICS)]
            totals[metric] += int(raw)
    return totals


async def _get_live_project_usage(project_id: str) -> dict[str, int]:
    redis = await get_redis()
    pipe = redis.pipeline()
    for metric in TRACKED_METRICS:
        pipe.get(f"usage:{project_id}:{metric}")
    values = await pipe.execute()
    return {
        metric: int(value) if value else 0
        for metric, value in zip(TRACKED_METRICS, values)
    }


# ─── Plans catalogue — no project needed ─────────────────────────────────────

@router.get("/billing/plans", dependencies=[InternalGuard])
async def list_plans(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return all plan limits from the DB. Falls back to hardcoded defaults if table missing."""
    try:
        result = await db.execute(text("SELECT * FROM plan_limits ORDER BY price_ngn ASC"))
        plans = [dict(r) for r in result.mappings()]
        if plans:
            return {"data": plans}
    except Exception as e:
        logger.warning("plan_limits table not yet migrated: %s", e)

    # Fallback defaults matching migration 005 seed data
    return {
        "data": [
            {
                "plan": "free",
                "sql_rows": 50_000,
                "nosql_docs": 50_000,
                "storage_bytes": 1_073_741_824,      # 1 GB
                "function_calls": 100_000,
                "ai_requests": 500,
                "api_calls_per_min": 60,
                "team_members": 1,
                "price_ngn": 0,
                "price_usd": 0,
            },
            {
                "plan": "starter",
                "sql_rows": 500_000,
                "nosql_docs": 500_000,
                "storage_bytes": 10_737_418_240,     # 10 GB
                "function_calls": 1_000_000,
                "ai_requests": 5_000,
                "api_calls_per_min": 300,
                "team_members": 3,
                "price_ngn": 15_000,
                "price_usd": 10,
            },
            {
                "plan": "pro",
                "sql_rows": None,
                "nosql_docs": None,
                "storage_bytes": 107_374_182_400,    # 100 GB
                "function_calls": None,
                "ai_requests": None,
                "api_calls_per_min": 1_000,
                "team_members": 10,
                "price_ngn": 45_000,
                "price_usd": 30,
            },
        ]
    }


# ─── Billing overview ─────────────────────────────────────────────────────────

@router.get("/billing/{project_id}/overview", dependencies=[InternalGuard])
async def billing_overview(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return plan, subscription, invoices summary, and project usage totals."""
    org_id = await _get_org_id_from_project(db, project_id)
    subscription = await _get_or_create_subscription(db, org_id)

    invoices_result = await db.execute(
        text("""
            SELECT id, amount_ngn, amount_usd, status, payment_ref,
                   period_start, period_end, created_at
            FROM invoices
            WHERE organization_id = :org_id
            ORDER BY created_at DESC
            LIMIT 12
        """),
        {"org_id": org_id},
    )
    invoices = [_serialize(dict(r)) for r in invoices_result.mappings()]

    org_result = await db.execute(
        text("SELECT plan FROM organizations WHERE id = :org_id"),
        {"org_id": org_id},
    )
    org_row = org_result.mappings().first()
    plan = org_row["plan"] if org_row else "free"

    # 30-day rolling usage across all projects in the org
    usage_result = await db.execute(
        text("""
            SELECT
                COALESCE(SUM(value) FILTER (WHERE metric = 'db_reads'),       0) AS db_reads,
                COALESCE(SUM(value) FILTER (WHERE metric = 'db_writes'),      0) AS db_writes,
                COALESCE(SUM(value) FILTER (WHERE metric = 'nosql_reads'),    0) AS nosql_reads,
                COALESCE(SUM(value) FILTER (WHERE metric = 'nosql_writes'),   0) AS nosql_writes,
                COALESCE(SUM(value) FILTER (WHERE metric = 'storage_bytes'),  0) AS storage_bytes,
                COALESCE(SUM(value) FILTER (WHERE metric = 'function_calls'), 0) AS function_calls,
                COALESCE(SUM(value) FILTER (WHERE metric = 'ai_requests'),    0) AS ai_requests
            FROM usage_records ur
            JOIN projects p ON p.id = ur.project_id
            WHERE p.organization_id = :org_id
              AND ur.period_start >= NOW() - INTERVAL '30 days'
        """),
        {"org_id": org_id},
    )
    usage_row = usage_result.mappings().first()
    usage = {metric: int(usage_row.get(metric) or 0) for metric in TRACKED_METRICS} if usage_row else {metric: 0 for metric in TRACKED_METRICS}

    # Add live Redis counters for the current flush window so the dashboard reflects near-real-time usage.
    try:
        live_usage = await _get_live_org_usage(org_id)
        for metric, value in live_usage.items():
            usage[metric] = usage.get(metric, 0) + value
    except Exception:
        pass

    return {
        "data": {
            "plan": plan,
            "subscription": subscription,
            "invoices": invoices,
            "usage": usage,
        }
    }


# ─── Per-project usage with limits ────────────────────────────────────────────

@router.get("/billing/usage/{project_id}", dependencies=[InternalGuard])
async def project_usage(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """30-day rolling usage for a single project, with plan limits resolved from DB."""
    # Usage from usage_records
    result = await db.execute(
        text("""
            SELECT metric, SUM(value)::bigint AS total
            FROM usage_records
            WHERE project_id = :project_id
              AND period_start >= NOW() - INTERVAL '30 days'
            GROUP BY metric
        """),
        {"project_id": project_id},
    )
    usage = {metric: 0 for metric in TRACKED_METRICS}
    usage.update({r["metric"]: int(r["total"]) for r in result.mappings()})

    try:
        live_usage = await _get_live_project_usage(project_id)
        for metric, value in live_usage.items():
            usage[metric] = usage.get(metric, 0) + value
    except Exception:
        pass

    # Plan limits from DB (joined through org → plan_limits)
    try:
        limits_result = await db.execute(
            text("""
                SELECT pl.*
                FROM plan_limits pl
                JOIN organizations o ON o.plan = pl.plan
                JOIN projects p ON p.organization_id = o.id
                WHERE p.id = :project_id
            """),
            {"project_id": project_id},
        )
        limits_row = limits_result.mappings().first()
        limits = dict(limits_row) if limits_row else {}
    except Exception:
        limits = {}

    return {"data": {"usage": usage, "limits": limits}}


# ─── Subscription detail ──────────────────────────────────────────────────────

@router.get("/billing/{project_id}/subscription", dependencies=[InternalGuard])
async def get_subscription(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    org_id = await _get_org_id_from_project(db, project_id)
    sub = await _get_or_create_subscription(db, org_id)
    return {"data": sub}


# ─── Flutterwave checkout initiation ─────────────────────────────────────────

class CheckoutInitiateRequest(BaseModel):
    plan: str = Field(pattern="^(starter|pro)$")
    user_email: str
    user_name: str
    currency: str = "NGN"
    redirect_url: str


@router.post("/billing/{project_id}/checkout/initiate", dependencies=[InternalGuard])
async def initiate_checkout(
    project_id: str,
    body: CheckoutInitiateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not settings.flutterwave_secret_key:
        raise HTTPException(
            status_code=503,
            detail="Payment provider not configured. Set FLUTTERWAVE_SECRET_KEY.",
        )

    org_id = await _get_org_id_from_project(db, project_id)

    # Fetch price from DB plan_limits
    try:
        plan_result = await db.execute(
            text("SELECT price_ngn, price_usd FROM plan_limits WHERE plan = :plan"),
            {"plan": body.plan},
        )
        plan_row = plan_result.mappings().first()
    except Exception:
        plan_row = None

    if not plan_row:
        # Fallback hardcoded prices
        fallback = {"starter": {"price_ngn": 15000, "price_usd": 10}, "pro": {"price_ngn": 45000, "price_usd": 30}}
        plan_row = fallback.get(body.plan)  # type: ignore[assignment]
        if not plan_row:
            raise HTTPException(status_code=400, detail="Unknown plan")

    amount = float(plan_row["price_ngn"]) if body.currency == "NGN" else float(plan_row["price_usd"])
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Cannot checkout for a free plan")

    # tx_ref format: baas_proj_{project_id}_{plan}_{random8}
    tx_ref = f"baas_proj_{project_id}_{body.plan}_{uuid.uuid4().hex[:8]}"

    flw_payload = {
        "tx_ref": tx_ref,
        "amount": amount,
        "currency": body.currency,
        "redirect_url": body.redirect_url,
        "customer": {"email": body.user_email, "name": body.user_name},
        "customizations": {
            "title": "YourBaaS",
            "description": f"Upgrade to {body.plan.title()} plan",
            "logo": "https://yourbaas.com/logo.png",
        },
        "meta": {"project_id": project_id, "org_id": org_id, "plan": body.plan},
        "payment_options": "card,ussd,banktransfer",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.flutterwave.com/v3/payments",
                headers={
                    "Authorization": f"Bearer {settings.flutterwave_secret_key}",
                    "Content-Type": "application/json",
                },
                json=flw_payload,
            )
            data = resp.json()
    except Exception as e:
        logger.error("Flutterwave checkout initiation failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment provider unreachable")

    if data.get("status") != "success":
        logger.error("Flutterwave error response: %s", data)
        raise HTTPException(
            status_code=400,
            detail=data.get("message", "Checkout initiation failed"),
        )

    checkout_url = data["data"]["link"]

    # Log the initiation event
    try:
        await db.execute(
            text("""
                INSERT INTO billing_events
                    (id, organization_id, event_type, flw_tx_ref, amount, currency, status)
                VALUES (:id, :org_id, 'checkout.initiated', :tx_ref, :amount, :currency, 'pending')
            """),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "tx_ref": tx_ref,
                "amount": amount,
                "currency": body.currency,
            },
        )
        await db.commit()
    except Exception as e:
        logger.warning("Failed to log billing event: %s", e)

    return {"data": {"checkout_url": checkout_url, "tx_ref": tx_ref}}


# ─── Verify a completed transaction ──────────────────────────────────────────

class VerifyRequest(BaseModel):
    tx_ref: str
    transaction_id: str


@router.post("/billing/{project_id}/checkout/verify", dependencies=[InternalGuard])
async def verify_checkout(
    project_id: str,
    body: VerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not settings.flutterwave_secret_key:
        raise HTTPException(status_code=503, detail="Payment provider not configured")

    org_id = await _get_org_id_from_project(db, project_id)

    # Verify with Flutterwave API
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"https://api.flutterwave.com/v3/transactions/{body.transaction_id}/verify",
                headers={"Authorization": f"Bearer {settings.flutterwave_secret_key}"},
            )
            data = resp.json()
    except Exception as e:
        logger.error("Flutterwave verify failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not verify payment with Flutterwave")

    tx = data.get("data", {})
    if data.get("status") != "success" or tx.get("status") != "successful":
        raise HTTPException(status_code=400, detail="Payment not completed successfully")

    if tx.get("tx_ref") != body.tx_ref:
        raise HTTPException(status_code=400, detail="Transaction reference mismatch")

    # tx_ref format: baas_proj_{project_id}_{plan}_{random8}
    # Split safely: ["baas", "proj", project_id, plan, random8]
    parts = body.tx_ref.split("_")
    if len(parts) < 5:
        raise HTTPException(status_code=400, detail="Invalid transaction reference format")

    new_plan = parts[3]
    if new_plan not in ("starter", "pro"):
        raise HTTPException(status_code=400, detail=f"Invalid plan in tx_ref: {new_plan}")

    amount_ngn = float(tx.get("amount", 0)) if tx.get("currency") == "NGN" else 0.0
    amount_usd = float(tx.get("amount", 0)) if tx.get("currency") == "USD" else 0.0
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=30)
    tx_id_str = str(body.transaction_id)

    # Update org plan
    await db.execute(
        text("UPDATE organizations SET plan = :plan WHERE id = :org_id"),
        {"plan": new_plan, "org_id": org_id},
    )

    # Upsert subscription — uses flw_tx_id (added in migration 006)
    await db.execute(
        text("""
            INSERT INTO subscriptions
                (id, organization_id, plan, status, flw_tx_ref, flw_tx_id,
                 current_period_start, current_period_end, amount_ngn, amount_usd,
                 cancel_at_period_end)
            VALUES
                (:id, :org_id, :plan, 'active', :tx_ref, :tx_id,
                 :period_start, :period_end, :amount_ngn, :amount_usd, false)
            ON CONFLICT (organization_id) DO UPDATE SET
                plan                 = EXCLUDED.plan,
                status               = 'active',
                flw_tx_ref           = EXCLUDED.flw_tx_ref,
                flw_tx_id            = EXCLUDED.flw_tx_id,
                current_period_start = EXCLUDED.current_period_start,
                current_period_end   = EXCLUDED.current_period_end,
                amount_ngn           = EXCLUDED.amount_ngn,
                amount_usd           = EXCLUDED.amount_usd,
                cancel_at_period_end = false,
                updated_at           = NOW()
        """),
        {
            "id": str(uuid.uuid4()),
            "org_id": org_id,
            "plan": new_plan,
            "tx_ref": body.tx_ref,
            "tx_id": tx_id_str,
            "period_start": now,
            "period_end": period_end,
            "amount_ngn": amount_ngn,
            "amount_usd": amount_usd,
        },
    )

    # Create invoice record
    invoice_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO invoices
                (id, organization_id, amount_ngn, amount_usd, status,
                 payment_ref, period_start, period_end)
            VALUES
                (:id, :org_id, :amount_ngn, :amount_usd, 'paid',
                 :payment_ref, :period_start, :period_end)
        """),
        {
            "id": invoice_id,
            "org_id": org_id,
            "amount_ngn": amount_ngn,
            "amount_usd": amount_usd,
            "payment_ref": tx_id_str,
            "period_start": now,
            "period_end": period_end,
        },
    )

    # Log billing event
    try:
        await db.execute(
            text("""
                INSERT INTO billing_events
                    (id, organization_id, event_type, flw_tx_id, flw_tx_ref,
                     amount, currency, status, payload)
                VALUES
                    (:id, :org_id, 'charge.completed', :tx_id, :tx_ref,
                     :amount, :currency, 'successful', :payload)
            """),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "tx_id": tx_id_str,
                "tx_ref": body.tx_ref,
                "amount": float(tx.get("amount", 0)),
                "currency": tx.get("currency", "NGN"),
                "payload": json.dumps(tx),
            },
        )
    except Exception as e:
        logger.warning("Failed to log billing event: %s", e)

    await db.commit()

    # Bust cached API keys for this project so plan change takes effect immediately
    try:
        from app.db.redis import get_redis
        redis = await get_redis()
        async for cache_key in redis.scan_iter("apikey:*"):
            cached = await redis.get(cache_key)
            if cached:
                d = json.loads(cached)
                if d.get("project_id") == project_id:
                    await redis.delete(cache_key)
        # Also bust plan limits cache
        await redis.delete(f"plan_limits:{project_id}")
    except Exception as e:
        logger.warning("Failed to bust caches after plan upgrade: %s", e)

    return {"data": {"verified": True, "plan": new_plan, "invoice_id": invoice_id}}


# ─── Cancel subscription ──────────────────────────────────────────────────────

@router.post("/billing/{project_id}/cancel", dependencies=[InternalGuard])
async def cancel_subscription(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    org_id = await _get_org_id_from_project(db, project_id)
    await db.execute(
        text("""
            UPDATE subscriptions
            SET cancel_at_period_end = true, updated_at = NOW()
            WHERE organization_id = :org_id
        """),
        {"org_id": org_id},
    )
    await db.commit()
    return {"data": {"cancel_at_period_end": True}}


# ─── Flutterwave webhook (public — no internal secret) ────────────────────────

@router.post("/webhooks/flutterwave")
async def flutterwave_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    verif_hash: str = Header(default="", alias="verif-hash"),
) -> dict[str, Any]:
    """
    Receive Flutterwave webhook events.
    Validates the verif-hash header against FLUTTERWAVE_WEBHOOK_HASH env var.
    """
    if settings.flutterwave_webhook_hash and verif_hash != settings.flutterwave_webhook_hash:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    body_bytes = await request.body()
    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = payload.get("event", "")
    data = payload.get("data", {})
    tx_ref: str = data.get("tx_ref", "")
    tx_id = str(data.get("id", ""))
    status = data.get("status", "")

    logger.info("Flutterwave webhook: event=%s tx_ref=%s status=%s", event, tx_ref, status)

    # Resolve org from tx_ref: baas_proj_{project_id}_{plan}_{random8}
    org_id: str | None = None
    project_id_from_ref: str | None = None
    new_plan: str | None = None

    if tx_ref.startswith("baas_proj_"):
        parts = tx_ref.split("_")
        # Expected: ["baas", "proj", {project_id}, {plan}, {random8}]
        if len(parts) >= 5:
            project_id_from_ref = parts[2]
            new_plan = parts[3]
            try:
                org_id = await _get_org_id_from_project(db, project_id_from_ref)
            except HTTPException:
                logger.warning(
                    "Webhook: project %s from tx_ref %s not found",
                    project_id_from_ref, tx_ref,
                )

    # Always log the raw event for audit trail
    try:
        await db.execute(
            text("""
                INSERT INTO billing_events
                    (id, organization_id, event_type, flw_tx_id, flw_tx_ref,
                     amount, currency, status, payload)
                VALUES
                    (:id, :org_id, :event_type, :tx_id, :tx_ref,
                     :amount, :currency, :status, :payload)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "event_type": event,
                "tx_id": tx_id,
                "tx_ref": tx_ref,
                "amount": float(data.get("amount", 0)),
                "currency": data.get("currency", "NGN"),
                "status": status,
                "payload": json.dumps(payload),
            },
        )
    except Exception as e:
        logger.warning("Failed to log billing event from webhook: %s", e)

    # Handle successful charge
    if event == "charge.completed" and status == "successful" and org_id and new_plan:
        if new_plan not in ("starter", "pro"):
            logger.warning("Webhook: unknown plan %s in tx_ref %s", new_plan, tx_ref)
            await db.commit()
            return {"status": "ok"}

        amount_ngn = float(data.get("amount", 0)) if data.get("currency") == "NGN" else 0.0
        amount_usd = float(data.get("amount", 0)) if data.get("currency") == "USD" else 0.0
        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=30)

        try:
            await db.execute(
                text("UPDATE organizations SET plan = :plan WHERE id = :org_id"),
                {"plan": new_plan, "org_id": org_id},
            )
            await db.execute(
                text("""
                    INSERT INTO subscriptions
                        (id, organization_id, plan, status, flw_tx_ref, flw_tx_id,
                         current_period_start, current_period_end, amount_ngn, amount_usd,
                         cancel_at_period_end)
                    VALUES
                        (:id, :org_id, :plan, 'active', :tx_ref, :tx_id,
                         :period_start, :period_end, :amount_ngn, :amount_usd, false)
                    ON CONFLICT (organization_id) DO UPDATE SET
                        plan                 = EXCLUDED.plan,
                        status               = 'active',
                        flw_tx_ref           = EXCLUDED.flw_tx_ref,
                        flw_tx_id            = EXCLUDED.flw_tx_id,
                        current_period_start = EXCLUDED.current_period_start,
                        current_period_end   = EXCLUDED.current_period_end,
                        amount_ngn           = EXCLUDED.amount_ngn,
                        amount_usd           = EXCLUDED.amount_usd,
                        updated_at           = NOW()
                """),
                {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "plan": new_plan,
                    "tx_ref": tx_ref,
                    "tx_id": tx_id,
                    "period_start": now,
                    "period_end": period_end,
                    "amount_ngn": amount_ngn,
                    "amount_usd": amount_usd,
                },
            )
            await db.execute(
                text("""
                    INSERT INTO invoices
                        (id, organization_id, amount_ngn, amount_usd, status,
                         payment_ref, period_start, period_end)
                    VALUES
                        (:id, :org_id, :amount_ngn, :amount_usd, 'paid',
                         :payment_ref, :period_start, :period_end)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "amount_ngn": amount_ngn,
                    "amount_usd": amount_usd,
                    "payment_ref": tx_id,
                    "period_start": now,
                    "period_end": period_end,
                },
            )
            logger.info(
                "Webhook: upgraded org %s to plan %s (tx=%s)", org_id, new_plan, tx_id
            )
        except Exception as e:
            logger.error("Webhook: failed to update plan for org %s: %s", org_id, e)

    await db.commit()
    return {"status": "ok"}