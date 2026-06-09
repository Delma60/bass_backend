from typing import Any

from pydantic import BaseModel, EmailStr, Field


# ─── Auth ─────────────────────────────────────────────────────────────────────

class SignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str | None = None


class SignInRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


# ─── SQL Database ─────────────────────────────────────────────────────────────

class InsertRowRequest(BaseModel):
    # Accept either a single row (dict) or a list of rows. Cap the list size
    # to prevent DoS via extremely large payloads.
    data: dict[str, Any] | list[dict[str, Any]] = Field(..., max_items=1000)


class UpdateRowRequest(BaseModel):
    data: dict[str, Any]


class RpcCallRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


# ─── NoSQL ────────────────────────────────────────────────────────────────────

class InsertDocumentRequest(BaseModel):
    data: dict[str, Any] | list[dict[str, Any]]


class UpdateDocumentRequest(BaseModel):
    update: dict[str, Any]  # MongoDB update operators e.g. {"$set": {...}}


class AggregationRequest(BaseModel):
    pipeline: list[dict[str, Any]]


# ─── Key-Value ────────────────────────────────────────────────────────────────

class KVSetRequest(BaseModel):
    value: Any
    ttl: int | None = None  # seconds


class KVBatchRequest(BaseModel):
    operations: list[dict[str, Any]]  # [{op: "get"|"set"|"delete", key: str, value?: Any}]


# ─── Storage ──────────────────────────────────────────────────────────────────

class PresignedUploadRequest(BaseModel):
    filename: str
    content_type: str
    expires_in: int = Field(default=3600, ge=60, le=86400)


# ─── Realtime ─────────────────────────────────────────────────────────────────

class RealtimeSubscribeRequest(BaseModel):
    table_or_collection: str
    event_types: list[str] = Field(default_factory=lambda: ["INSERT", "UPDATE", "DELETE"])


# ─── Functions ────────────────────────────────────────────────────────────────

class FunctionInvokeRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


# ─── Superadmin ───────────────────────────────────────────────────────────────

class StaffInviteRequest(BaseModel):
    email: EmailStr
    name: str
    role: str  # super_admin | ops | billing | support


class StaffUpdateRoleRequest(BaseModel):
    role: str


class UserUpdateRequest(BaseModel):
    is_banned: bool | None = None
    is_email_verified: bool | None = None


class OrgPlanOverrideRequest(BaseModel):
    plan: str  # free | starter | pro
    reason: str | None = None


class ProjectStatusRequest(BaseModel):
    status: str  # active | paused


class FeatureFlagUpdateRequest(BaseModel):
    enabled: bool
    description: str | None = None