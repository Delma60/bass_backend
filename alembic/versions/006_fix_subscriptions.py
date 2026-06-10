"""add flw_tx_id column to subscriptions and align billing tables

Revision ID: 006_fix_subscriptions
Revises: 005_billing_flutterwave
Create Date: 2026-06-10

Fixes:
- subscriptions table was missing flw_tx_id column (referenced in billing_browse.py)
- adds admin_integration_secret support
"""
from alembic import op

revision = "006_fix_subscriptions"
down_revision = "005_billing_flutterwave"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add missing flw_tx_id column to subscriptions
    op.execute("""
        ALTER TABLE subscriptions
        ADD COLUMN IF NOT EXISTS flw_tx_id TEXT
    """)

    # Add index for faster lookup by Flutterwave tx id
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscriptions_flw_tx_id
        ON subscriptions(flw_tx_id)
        WHERE flw_tx_id IS NOT NULL
    """)

    # Add missing description column to projects if not present (for UI)
    op.execute("""
        ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS description TEXT
    """)

    # Ensure billing_events has the indexes needed for webhook deduplication
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_events_flw_tx_id_type
        ON billing_events(flw_tx_id, event_type)
        WHERE flw_tx_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_billing_events_flw_tx_id_type")
    op.execute("DROP INDEX IF EXISTS idx_subscriptions_flw_tx_id")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS flw_tx_id")