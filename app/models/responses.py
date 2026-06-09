from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Meta(BaseModel):
    count: int = 0
    page: int = 1
    limit: int = 100


class DataResponse(BaseModel, Generic[T]):
    data: T
    meta: Meta | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ─── Auth ─────────────────────────────────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: str
    email: str
    name: str | None = None
    is_email_verified: bool = False
    created_at: str


# ─── SQL ──────────────────────────────────────────────────────────────────────

class RowResponse(BaseModel):
    id: str | int | None = None
    data: dict[str, Any]


# ─── NoSQL ────────────────────────────────────────────────────────────────────

class DocumentResponse(BaseModel):
    id: str
    data: dict[str, Any]


# ─── KV ───────────────────────────────────────────────────────────────────────

class KVEntryResponse(BaseModel):
    key: str
    value: Any
    ttl: int | None = None


# ─── Storage ──────────────────────────────────────────────────────────────────

class PresignedUploadResponse(BaseModel):
    upload_url: str
    file_url: str
    key: str
    expires_in: int


class FileMetaResponse(BaseModel):
    key: str
    size: int
    content_type: str | None = None
    last_modified: str | None = None
    url: str | None = None


# ─── Superadmin ───────────────────────────────────────────────────────────────

class PlatformMetricsResponse(BaseModel):
    total_users: int
    total_organizations: int
    total_projects: int
    active_projects: int
    monthly_revenue_ngn: float
    monthly_revenue_usd: float


class StaffResponse(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_active: bool
    last_login_at: str | None = None
    created_at: str


class AuditLogResponse(BaseModel):
    id: str
    actor_id: str
    actor_role: str
    action: str
    resource: str | None = None
    meta: dict[str, Any] | None = None
    created_at: str