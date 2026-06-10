"""add subscriptions, billing_events, and usage_limits tables

Revision ID: 005_billing_flutterwave
Revises: 004_notifications
Create Date: 2026-06-09
"""
from alembic import op

revision = "005_billing_flutterwave"
down_revision = "004_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── subscriptions ──────────────────────────────────────────────────────────
    # One active subscription per organization.  Tracks which plan the org is on,
    # the Flutterwave customer/subscription references, and renewal metadata.
    op.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id                  TEXT PRIMARY KEY,
            organization_id     TEXT NOT NULL UNIQUE REFERENCES organizations(id) ON DELETE CASCADE,
            plan                TEXT NOT NULL DEFAULT 'free',
            status              TEXT NOT NULL DEFAULT 'active',
            -- Flutterwave references
            flw_customer_id     TEXT,
            flw_subscription_id TEXT,
            flw_plan_id         TEXT,
            flw_tx_ref          TEXT,
            -- Billing cycle
            current_period_start TIMESTAMPTZ,
            current_period_end   TIMESTAMPTZ,
            cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
            -- Meta
            currency            TEXT NOT NULL DEFAULT 'NGN',
            amount_ngn          NUMERIC NOT NULL DEFAULT 0,
            amount_usd          NUMERIC NOT NULL DEFAULT 0,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_org ON subscriptions(organization_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status)")

    # ── billing_events ─────────────────────────────────────────────────────────
    # Immutable log of every payment / webhook event.
    op.execute("""
        CREATE TABLE IF NOT EXISTS billing_events (
            id              TEXT PRIMARY KEY,
            organization_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
            event_type      TEXT NOT NULL,  -- 'charge.completed', 'subscription.activated', etc.
            flw_tx_id       TEXT,
            flw_tx_ref      TEXT,
            amount          NUMERIC,
            currency        TEXT,
            status          TEXT,           -- 'successful', 'failed', 'pending'
            payload         TEXT,           -- raw Flutterwave webhook JSON
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_billing_events_org ON billing_events(organization_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_billing_events_type ON billing_events(event_type)")

    # ── plan_limits ────────────────────────────────────────────────────────────
    # Static reference table — keyed by plan name.  Read by the enforcement middleware.
    op.execute("""
        CREATE TABLE IF NOT EXISTS plan_limits (
            plan                TEXT PRIMARY KEY,
            sql_rows            BIGINT,          -- NULL = unlimited
            nosql_docs          BIGINT,
            storage_bytes       BIGINT,
            function_calls      BIGINT,
            ai_requests         BIGINT,
            api_calls_per_min   INTEGER NOT NULL DEFAULT 60,
            team_members        INTEGER NOT NULL DEFAULT 1,
            price_ngn           NUMERIC NOT NULL DEFAULT 0,
            price_usd           NUMERIC NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        INSERT INTO plan_limits (plan, sql_rows, nosql_docs, storage_bytes, function_calls, ai_requests, api_calls_per_min, team_members, price_ngn, price_usd)
        VALUES
            ('free',    50000,   50000,   1073741824,   100000,  500,   60,  1,  0,     0),
            ('starter', 500000,  500000,  10737418240,  1000000, 5000,  300, 3,  15000, 10),
            ('pro',     NULL,    NULL,    107374182400, NULL,    NULL,  1000,10, 45000, 30)
        ON CONFLICT (plan) DO UPDATE SET
            sql_rows          = EXCLUDED.sql_rows,
            nosql_docs        = EXCLUDED.nosql_docs,
            storage_bytes     = EXCLUDED.storage_bytes,
            function_calls    = EXCLUDED.function_calls,
            ai_requests       = EXCLUDED.ai_requests,
            api_calls_per_min = EXCLUDED.api_calls_per_min,
            team_members      = EXCLUDED.team_members,
            price_ngn         = EXCLUDED.price_ngn,
            price_usd         = EXCLUDED.price_usd
    """)

    # ── project_usage_cache ────────────────────────────────────────────────────
    # Materialised 30-day rolling usage per project — updated by Celery hourly.
    # The enforcement middleware reads from here (cheap, no aggregation on hot path).
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_usage_cache (
            project_id  TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            db_reads    BIGINT NOT NULL DEFAULT 0,
            db_writes   BIGINT NOT NULL DEFAULT 0,
            nosql_reads BIGINT NOT NULL DEFAULT 0,
            nosql_writes BIGINT NOT NULL DEFAULT 0,
            storage_bytes BIGINT NOT NULL DEFAULT 0,
            function_calls BIGINT NOT NULL DEFAULT 0,
            ai_requests  BIGINT NOT NULL DEFAULT 0,
            api_calls    BIGINT NOT NULL DEFAULT 0,
            refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    # ── auto-create free subscription for every existing org ──────────────────
    op.execute("""
        INSERT INTO subscriptions (id, organization_id, plan, status, currency)
        SELECT gen_random_uuid()::text, o.id, o.plan, 'active', 'NGN'
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM subscriptions s WHERE s.organization_id = o.id
        )
        ON CONFLICT (organization_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_usage_cache")
    op.execute("DROP TABLE IF EXISTS plan_limits")
    op.execute("DROP TABLE IF EXISTS billing_events")
    op.execute("DROP TABLE IF EXISTS subscriptions")