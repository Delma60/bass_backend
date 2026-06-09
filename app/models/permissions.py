from typing import Any, Literal

from pydantic import BaseModel


class PermissionRule(BaseModel):
    operation: Literal["SELECT", "INSERT", "UPDATE", "DELETE", "FIND", "AGGREGATE"]
    allow: Literal["public", "authenticated", "owner"]
    condition: str | dict[str, Any] | None = None


class ResourcePermissions(BaseModel):
    resource: str
    engine: Literal["sql", "nosql", "kv"]
    rules: list[PermissionRule]


class AuthContext(BaseModel):
    """Represents the authenticated user context during a request."""
    uid: str | None = None
    email: str | None = None
    role: str | None = None
    is_authenticated: bool = False
    project_id: str | None = None