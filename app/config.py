from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[BACKEND_ROOT / ".env", REPO_ROOT / ".env"],
        extra="ignore",
    )

    # App
    app_name: str = "BaaS Platform"
    node_env: str = "development"
    fastapi_base_url: str = "http://localhost:8000"
    internal_api_secret: str = Field(..., min_length=32)

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/baas_platform"
    database_sync_url: str = "postgresql://postgres:postgres@localhost:5432/baas_platform"
    # Optional restricted DB role to SET LOCAL ROLE to before executing tenant SQL.
    # Create this role in Postgres and grant minimal permissions (SELECT/INSERT/UPDATE/DELETE
    # on tenant schemas only). Leave empty to skip role switching.
    db_restricted_role: str | None = None
    # Toggle to indicate RLS/role-switching is desired (no effect if db_restricted_role is None)
    db_enable_rls: bool = False

    # MongoDB
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_db_name: str = "baas_platform"

    # Redis
    redis_url: str = "redis://localhost:6379"
    # API key cache TTL (seconds) for Redis. Lower values reduce revocation window.
    api_key_cache_ttl: int = 15

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_public_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    region: str = "us-east-1"
    minio_use_ssl: bool = True
    storage_cors_allowed_origins: str = "http://localhost:3000"

    # Auth.js / per-project JWTs
    jwt_secret: str = Field(..., min_length=32)
    jwt_expiry: int = 3600

    # Staff / Superadmin
    staff_jwt_secret: str = Field(..., min_length=32)
    staff_jwt_expiry: int = 28800
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "changeme"

    # API key encryption
    api_key_encryption_secret: str = Field(..., min_length=32)

    # Email
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_secure: bool = False

    # Payments
    paystack_secret_key: str = ""
    paystack_public_key: str = ""
    paystack_webhook_secret: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    flutterwave_secret_key: str = ""
    flutterwave_public_key: str = ""
    flutterwave_webhook_hash: str = ""

    # AI
    openai_api_key: str = ""


settings = Settings()