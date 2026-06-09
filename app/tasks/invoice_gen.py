# backend/app/tasks/invoice_gen.py
"""
Invoice generation task — runs on the 1st of each month.

Flow:
1. For each organization on a paid plan, calculate usage for the prior month.
2. Generate an invoice record in Postgres.
3. Trigger payment via Paystack (primary) or Stripe (secondary).
4. Send invoice email via SMTP.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

PLAN_PRICES = {
    "starter": {"ngn": 15_000, "usd": 10},
    "pro":     {"ngn": 45_000, "usd": 30},
}


def _run_async(coro):  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _generate_invoices_async() -> dict:
    from sqlalchemy import text
    from app.db.postgres import AsyncSessionLocal

    now = datetime.now(timezone.utc)
    period_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_start = period_end - relativedelta(months=1)

    generated = 0
    errors = 0

    async with AsyncSessionLocal() as session:
        # Fetch all paid orgs
        result = await session.execute(
            text("SELECT id, name, plan FROM organizations WHERE plan IN ('starter', 'pro')")
        )
        orgs = result.mappings().all()

        for org in orgs:
            try:
                invoice_id = str(uuid.uuid4())
                prices = PLAN_PRICES[org["plan"]]

                # Check idempotency — don't double-invoice
                existing = await session.execute(
                    text("""
                        SELECT id FROM invoices
                        WHERE organization_id = :org_id
                          AND period_start = :period_start
                          AND period_end = :period_end
                    """),
                    {"org_id": org["id"], "period_start": period_start, "period_end": period_end},
                )
                if existing.first():
                    logger.info("Invoice already exists for org %s period %s", org["id"], period_start)
                    continue

                await session.execute(
                    text("""
                        INSERT INTO invoices (
                            id, organization_id, amount_ngn, amount_usd,
                            status, period_start, period_end, created_at
                        )
                        VALUES (
                            :id, :org_id, :amount_ngn, :amount_usd,
                            'pending', :period_start, :period_end, NOW()
                        )
                    """),
                    {
                        "id": invoice_id,
                        "org_id": org["id"],
                        "amount_ngn": prices["ngn"],
                        "amount_usd": prices["usd"],
                        "period_start": period_start,
                        "period_end": period_end,
                    },
                )
                generated += 1

                # Queue payment task — don't await in the loop
                charge_invoice.delay(invoice_id, org["id"], org["plan"])

            except Exception as e:
                logger.error("Failed to generate invoice for org %s: %s", org["id"], e)
                errors += 1

        await session.commit()

    logger.info("Generated %d invoice(s), %d error(s)", generated, errors)
    return {"generated": generated, "errors": errors}


async def _charge_invoice_async(invoice_id: str, org_id: str, plan: str) -> None:
    from sqlalchemy import text
    from app.db.postgres import AsyncSessionLocal
    from app.config import settings

    async with AsyncSessionLocal() as session:
        # Get org payment info
        result = await session.execute(
            text("SELECT paystack_customer_id, stripe_customer_id, billing_currency FROM organizations WHERE id = :org_id"),
            {"org_id": org_id},
        )
        org = result.mappings().first()

        prices = PLAN_PRICES.get(plan, {})
        payment_status = "failed"
        payment_ref: str | None = None

        try:
            if org and org.get("paystack_customer_id") and settings.paystack_secret_key:
                # Paystack charge
                import httpx
                amount_kobo = prices["ngn"] * 100  # Paystack uses kobo
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://api.paystack.co/transaction/charge_authorization",
                        headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
                        json={
                            "email": "",  # fetched via customer lookup in production
                            "amount": amount_kobo,
                            "authorization_code": org["paystack_customer_id"],
                            "reference": invoice_id,
                        },
                        timeout=30,
                    )
                    data = resp.json()
                    if data.get("data", {}).get("status") == "success":
                        payment_status = "paid"
                        payment_ref = data["data"].get("reference")

            elif org and org.get("stripe_customer_id") and settings.stripe_secret_key:
                # Stripe charge
                import httpx
                amount_cents = int(prices["usd"] * 100)
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        "https://api.stripe.com/v1/payment_intents",
                        headers={"Authorization": f"Bearer {settings.stripe_secret_key}"},
                        data={
                            "amount": str(amount_cents),
                            "currency": "usd",
                            "customer": org["stripe_customer_id"],
                            "confirm": "true",
                            "metadata[invoice_id]": invoice_id,
                        },
                        timeout=30,
                    )
                    data = resp.json()
                    if data.get("status") == "succeeded":
                        payment_status = "paid"
                        payment_ref = data.get("id")
            else:
                # No payment method on file — mark as pending for manual follow-up
                payment_status = "pending"

        except Exception as e:
            logger.error("Payment failed for invoice %s: %s", invoice_id, e)
            payment_status = "failed"

        # Update invoice status
        await session.execute(
            text("UPDATE invoices SET status = :status, payment_ref = :ref WHERE id = :id"),
            {"status": payment_status, "ref": payment_ref, "id": invoice_id},
        )
        await session.commit()

        if payment_status == "paid":
            send_invoice_email.delay(invoice_id)


@celery_app.task(name="app.tasks.invoice_gen.generate_monthly_invoices", bind=True, max_retries=2)
def generate_monthly_invoices(self) -> dict:  # type: ignore[no-untyped-def]
    """1st of each month: create invoice records for all paid organizations."""
    try:
        return _run_async(_generate_invoices_async())
    except Exception as exc:
        logger.error("generate_monthly_invoices failed: %s", exc)
        raise self.retry(exc=exc, countdown=300)


@celery_app.task(name="app.tasks.invoice_gen.charge_invoice", bind=True, max_retries=3)
def charge_invoice(self, invoice_id: str, org_id: str, plan: str) -> None:  # type: ignore[no-untyped-def]
    """Attempt payment for a single invoice via Paystack or Stripe."""
    try:
        _run_async(_charge_invoice_async(invoice_id, org_id, plan))
    except Exception as exc:
        logger.error("charge_invoice %s failed: %s", invoice_id, exc)
        raise self.retry(exc=exc, countdown=3600)  # retry after 1 hour


@celery_app.task(name="app.tasks.invoice_gen.send_invoice_email", ignore_result=True)
def send_invoice_email(invoice_id: str) -> None:
    """Send a payment confirmation email for a paid invoice."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from app.config import settings

    async def _fetch_and_send() -> None:
        from sqlalchemy import text
        from app.db.postgres import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT i.id, i.amount_ngn, i.amount_usd, i.period_start, i.period_end,
                           o.name AS org_name, u.email AS owner_email
                    FROM invoices i
                    JOIN organizations o ON o.id = i.organization_id
                    JOIN users u ON u.id = o.owner_id
                    WHERE i.id = :invoice_id
                """),
                {"invoice_id": invoice_id},
            )
            row = result.mappings().first()

        if not row or not settings.smtp_host:
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Payment confirmed – {row['org_name']}"
        msg["From"] = settings.smtp_user or "noreply@yourbaas.com"
        msg["To"] = row["owner_email"]

        body = f"""
        Hi {row['org_name']},

        Your payment of ₦{row['amount_ngn']:,.0f} (${row['amount_usd']:.2f} USD)
        for the period {row['period_start'].strftime('%b %Y')} has been confirmed.

        Invoice ID: {row['id']}

        Thank you for using YourBaaS.
        """
        msg.attach(MIMEText(body.strip(), "plain"))

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                if settings.smtp_secure:
                    server.starttls()
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_pass)
                server.sendmail(msg["From"], [msg["To"]], msg.as_string())
            logger.info("Invoice email sent for %s to %s", invoice_id, row["owner_email"])
        except Exception as e:
            logger.error("Failed to send invoice email for %s: %s", invoice_id, e)

    _run_async(_fetch_and_send())