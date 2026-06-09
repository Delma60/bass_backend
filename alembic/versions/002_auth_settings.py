"""add project_auth_settings and project_email_templates tables

Revision ID: 002_auth_settings
Revises: 001_realtime
Create Date: 2026-06-07
"""
from alembic import op
import sqlalchemy as sa

revision = "002_auth_settings"
down_revision = "001_realtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_auth_settings (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
            settings_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_project_auth_settings_project
        ON project_auth_settings(project_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS project_email_templates (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            template_key TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(project_id, template_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_project_email_templates_project
        ON project_email_templates(project_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_email_templates")
    op.execute("DROP TABLE IF EXISTS project_auth_settings")