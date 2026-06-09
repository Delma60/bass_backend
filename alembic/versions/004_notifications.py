"""add notifications table

Revision ID: 004_notifications
Revises: 003_edge_functions
Create Date: 2026-06-09
"""
from alembic import op

revision = "004_notifications"
down_revision = "003_edge_functions"
branch_labels = None
depends_on = None

VALID_TYPES = (
    "project.created",
    "project.paused",
    "project.resumed",
    "project.deleted",
    "billing.invoice_paid",
    "billing.invoice_failed",
    "billing.invoice_pending",
    "usage.limit_warning",
    "usage.limit_exceeded",
    "auth.new_signup",
    "system.announcement",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type        TEXT NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT,
            -- optional deep-link back to the relevant resource
            href        TEXT,
            -- extra structured metadata (JSON)
            meta        TEXT,
            is_read     BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
        ON notifications(user_id, is_read, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_notifications_user_created
        ON notifications(user_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notifications")