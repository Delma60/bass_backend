from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request
from jose import JWTError

from app.middleware.api_key import validate_api_key
from app.middleware.rate_limit import rate_limit
from app.models.permissions import AuthContext


async def get_project_context(
    request: Request,
    _key: dict = Depends(validate_api_key),
    _rate: None = Depends(rate_limit),
) -> dict[str, Any]:
    """Returns project context from request state (set by api_key middleware)."""
    return {
        "project_id": request.state.project_id,
        "db_schema": request.state.db_schema,
        "mongo_database": request.state.mongo_database,
        "key_type": request.state.key_type,
    }


async def get_auth_context(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthContext:
    """
    Tries to decode the user JWT from Authorization header.
    Returns an unauthenticated context if no user token is present.
    The API key Bearer token has already been stripped by validate_api_key;
    the user JWT is passed as X-User-Token.
    """
    user_token = request.headers.get("x-user-token")
    if not user_token:
        return AuthContext(is_authenticated=False)

    project_id = getattr(request.state, "project_id", None)
    if not project_id:
        return AuthContext(is_authenticated=False)

    from app.auth.project_auth import decode_project_token
    try:
        payload = decode_project_token(user_token, project_id)
        return AuthContext(
            uid=payload.get("sub"),
            email=payload.get("email"),
            role=payload.get("role"),
            is_authenticated=True,
            project_id=project_id,
        )
    except JWTError:
        return AuthContext(is_authenticated=False)


def parse_filters(filter_str: str | None = Query(default=None, alias="filter")) -> list[tuple[str, str, Any]]:
    """
    Parse filter query string.
    Format: col:op:value,col:op:value
    Example: status:eq:published,age:gte:18
    """
    if not filter_str:
        return []

    filters = []
    for part in filter_str.split(","):
        parts = part.strip().split(":", 2)
        if len(parts) == 3:
            col, op, val = parts
            # Try to cast value
            parsed_val: Any = val
            if val.lower() == "true":
                parsed_val = True
            elif val.lower() == "false":
                parsed_val = False
            elif val.lower() == "null":
                parsed_val = None
            else:
                try:
                    parsed_val = int(val)
                except ValueError:
                    try:
                        parsed_val = float(val)
                    except ValueError:
                        parsed_val = val
            filters.append((col, op, parsed_val))

    return filters


ProjectCtx = Annotated[dict[str, Any], Depends(get_project_context)]
AuthCtx = Annotated[AuthContext, Depends(get_auth_context)]
ParsedFilters = Annotated[list[tuple[str, Any, Any]], Depends(parse_filters)]


def require_key_type(*allowed: str):
    """Dependency factory to require the API key type for an endpoint.

    Usage: `ctx: ProjectCtx = Depends(require_key_type('service'))`
    """
    async def _dep(ctx: dict[str, Any] = Depends(get_project_context)) -> dict[str, Any]:
        key_type = ctx.get("key_type")
        if key_type not in allowed:
            raise HTTPException(status_code=403, detail="API key not authorized for this operation")
        return ctx

    return _dep