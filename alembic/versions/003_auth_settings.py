"""add edge_functions table

Revision ID: 003_edge_functions
Revises: 002_auth_settings
Create Date: 2026-06-07
"""
from alembic import op
import sqlalchemy as sa

revision = "003_edge_functions"
down_revision = "002_auth_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS edge_functions (
            id          TEXT PRIMARY KEY,
            project_id  TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            endpoint_url TEXT NOT NULL,
            method      TEXT NOT NULL DEFAULT 'POST',
            timeout_ms  INTEGER NOT NULL DEFAULT 30000,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            invoke_count BIGINT NOT NULL DEFAULT 0,
            last_invoked_at TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(project_id, name)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_edge_functions_project
        ON edge_functions(project_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS edge_function_logs (
            id              TEXT PRIMARY KEY,
            project_id      TEXT NOT NULL,
            function_id     TEXT NOT NULL REFERENCES edge_functions(id) ON DELETE CASCADE,
            function_name   TEXT NOT NULL,
            status_code     INTEGER,
            duration_ms     INTEGER,
            request_payload TEXT,
            response_body   TEXT,
            error           TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_edge_function_logs_function
        ON edge_function_logs(function_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_edge_function_logs_project
        ON edge_function_logs(project_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS edge_function_logs")
    op.execute("DROP TABLE IF EXISTS edge_functions")