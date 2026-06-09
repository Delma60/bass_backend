"""add realtime channels and rules tables

Revision ID: 001_realtime
Revises:
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa

revision = "001_realtime"
down_revision = "000_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS realtime_channels (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            access_rule TEXT NOT NULL DEFAULT 'auth != null',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            enable_presence BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(project_id, name)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_realtime_channels_project
        ON realtime_channels(project_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS realtime_rules (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE,
            rules_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS realtime_rules")
    op.execute("DROP TABLE IF EXISTS realtime_channels")