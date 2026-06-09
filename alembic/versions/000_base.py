"""create base platform tables

Revision ID: 000_base
Revises: 
Create Date: 2026-06-09
"""
from alembic import op

revision = "000_base"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            hashed_password TEXT NOT NULL,
            is_email_verified BOOLEAN NOT NULL DEFAULT FALSE,
            is_banned BOOLEAN NOT NULL DEFAULT FALSE,
            last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            owner_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            paystack_customer_id TEXT,
            stripe_customer_id TEXT,
            billing_currency TEXT DEFAULT 'NGN',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS organization_members (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'member',
            UNIQUE(organization_id, user_id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            region TEXT NOT NULL DEFAULT 'lagos',
            status TEXT NOT NULL DEFAULT 'active',
            db_schema TEXT NOT NULL UNIQUE,
            mongo_database TEXT NOT NULL UNIQUE,
            auth_jwt_secret TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_projects_organization ON projects(organization_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            key_hash TEXT NOT NULL UNIQUE,
            key_type TEXT NOT NULL DEFAULT 'anon',
            label TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_keys_project ON api_keys(project_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            value BIGINT NOT NULL DEFAULT 0,
            period_start TIMESTAMPTZ NOT NULL,
            period_end TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_records_project ON usage_records(project_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            amount_ngn NUMERIC NOT NULL DEFAULT 0,
            amount_usd NUMERIC NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            payment_ref TEXT,
            period_start TIMESTAMPTZ NOT NULL,
            period_end TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY,
            actor_id TEXT,
            actor_role TEXT,
            action TEXT NOT NULL,
            resource TEXT,
            meta TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            hashed_password TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            invited_by TEXT,
            last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS resource_permissions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            resource_name TEXT NOT NULL,
            engine TEXT NOT NULL,
            rules_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(project_id, resource_name, engine)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS feature_flags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            description TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    for table in [
        "feature_flags", "resource_permissions", "staff",
        "audit_logs", "invoices", "usage_records",
        "api_keys", "projects", "organization_members",
        "organizations", "users",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")