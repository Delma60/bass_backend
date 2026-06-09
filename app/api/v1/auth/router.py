# backend/app/api/v1/auth/router.py
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.auth.project_auth import (
    create_access_token,
    create_refresh_token,
    decode_project_token,
    hash_password,
    verify_password,
)
from app.db.postgres import get_db, set_tenant_session
from app.dependencies import AuthCtx, ProjectCtx
from app.models.requests import RefreshTokenRequest, SignInRequest, SignUpRequest
from jose import JWTError

router = APIRouter(prefix="/auth", tags=["Auth"])
logger = logging.getLogger(__name__)


@router.post("/{project_id}/signup", status_code=201)
async def sign_up(
    project_id: str,
    body: SignUpRequest,
    ctx: ProjectCtx,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    schema = ctx["db_schema"]
    await set_tenant_session(db, schema)

    # Check existing user
    result = await db.execute(
        text(f'SELECT id FROM "{schema}"."_auth_users" WHERE email = :email'),
        {"email": body.email},
    )
    if result.first():
        raise HTTPException(status_code=409, detail="Email already registered")

    user_id = str(uuid.uuid4())
    hashed = hash_password(body.password)

    await db.execute(
        text(f"""
            INSERT INTO "{schema}"."_auth_users" (id, email, name, hashed_password, is_email_verified)
            VALUES (:id, :email, :name, :pwd, false)
        """),
        {"id": user_id, "email": body.email, "name": body.name or "", "pwd": hashed},
    )
    await db.commit()

    access_token = create_access_token(subject=user_id, project_id=project_id)
    refresh_token = create_refresh_token(subject=user_id, project_id=project_id)

    return {
        "data": {
            "user": {"id": user_id, "email": body.email, "name": body.name},
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }
    }


@router.post("/{project_id}/signin")
async def sign_in(
    project_id: str,
    body: SignInRequest,
    ctx: ProjectCtx,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    schema = ctx["db_schema"]
    await set_tenant_session(db, schema)

    result = await db.execute(
        text(f'SELECT id, email, name, hashed_password FROM "{schema}"."_auth_users" WHERE email = :email'),
        {"email": body.email},
    )
    row = result.mappings().first()

    if not row or not verify_password(body.password, row["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = create_access_token(subject=row["id"], project_id=project_id)
    refresh_token = create_refresh_token(subject=row["id"], project_id=project_id)

    return {
        "data": {
            "user": {"id": row["id"], "email": row["email"], "name": row["name"]},
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }
    }


@router.post("/{project_id}/refresh")
async def refresh_token(
    project_id: str,
    body: RefreshTokenRequest,
    ctx: ProjectCtx,
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    try:
        payload = decode_project_token(body.refresh_token, project_id)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    uid = payload["sub"]
    access_token = create_access_token(subject=uid, project_id=project_id)
    refresh_token_new = create_refresh_token(subject=uid, project_id=project_id)

    return {
        "data": {
            "access_token": access_token,
            "refresh_token": refresh_token_new,
            "token_type": "bearer",
        }
    }


@router.get("/{project_id}/me")
async def get_me(
    project_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")

    if not auth.is_authenticated or not auth.uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    schema = ctx["db_schema"]
    await set_tenant_session(db, schema)
    result = await db.execute(
        text(f'SELECT id, email, name, is_email_verified, created_at FROM "{schema}"."_auth_users" WHERE id = :uid'),
        {"uid": auth.uid},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return {"data": dict(row)}


@router.post("/{project_id}/signout")
async def sign_out(
    project_id: str,
    ctx: ProjectCtx,
    auth: AuthCtx,
) -> dict[str, Any]:
    """Invalidate the current session. Client should discard tokens."""
    if ctx["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Project ID mismatch")
    # Stateless JWTs — client discards. Future: add a deny-list in Redis.
    return {"data": {"signed_out": True}}