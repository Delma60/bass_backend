# backend/app/api/internal/router.py
import logging
import re
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from .nosql_browse import router as nosql_browser
from .sql_browse import router as sql_browser
from .auth_browse import router as auth_browser
from .realtime_browse import router as realtime_browser
from .functions_browse import router as function_browser
from .storage_browse import router as storage_browser
from .settings_browse import router as settings_browser
from .auth_settings import router as auth_settings_router
from .realtime_data import router as realtime_data_router
from app.config import settings
from app.db.postgres import get_db
from .notifications import router as notification_router

from app.provisioner.sql_provisioner import (
    add_column,
    create_table,
    drop_column,
    drop_table,
    provision_project_schema,
    teardown_project_schema,
)
from app.provisioner.nosql_provisioner import (
    create_collection,
    drop_collection,
    provision_project_database,
    teardown_project_database,
)
from app.storage.minio import ensure_bucket_exists, get_bucket_name

router = APIRouter(tags=["Internal"])
logger = logging.getLogger(__name__)

router.include_router(nosql_browser)
router.include_router(sql_browser)
router.include_router(auth_browser)
router.include_router(realtime_browser)
router.include_router(auth_settings_router)
router.include_router(realtime_data_router)
router.include_router(storage_browser)
router.include_router(function_browser)
router.include_router(settings_browser)
router.include_router(notification_router)
# ─── Auth guard ───────────────────────────────────────────────────────────────

async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower())
    return cleaned.strip("-")[:50]


def generate_auth_jwt_secret() -> str:
    return secrets.token_urlsafe(32)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_or_create_personal_org(db: AsyncSession, user_id: str, user_name: str, org_name: str | None = None) -> str:
    """
    Find or create a personal organization for a platform user.
    Returns the org_id.
    """
    result = await db.execute(
        text("SELECT id FROM organizations WHERE owner_id = :user_id LIMIT 1"),
        {"user_id": user_id},
    )
    row = result.first()
    if row:
        return str(row[0])

    org_id = str(uuid.uuid4())
    final_org_name = org_name or f"{user_name}'s Organization"
    await db.execute(
        text("""
            INSERT INTO organizations (id, name, plan, owner_id)
            VALUES (:id, :name, 'free', :owner_id)
            ON CONFLICT DO NOTHING
        """),
        {"id": org_id, "name": final_org_name, "owner_id": user_id},
    )
    logger.info("Created personal org %s for user %s", org_id, user_id)
    return org_id


# ─── Project read helpers ─────────────────────────────────────────────────────

@router.get("/projects", dependencies=[InternalGuard])
async def list_projects(
    user_id: str | None = Query(default=None, description="Filter by owner user_id"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    List projects. If user_id is provided, returns only that user's projects.
    Otherwise returns all (superadmin use case — internal only).
    """
    if user_id:
        result = await db.execute(
            text("""
                SELECT p.id, p.name, p.status, p.region, p.db_schema,
                       p.mongo_database, p.created_at,
                       o.name AS organization_name
                FROM projects p
                JOIN organizations o ON o.id = p.organization_id
                WHERE o.owner_id = :user_id
                ORDER BY p.created_at DESC
            """),
            {"user_id": user_id},
        )
    else:
        result = await db.execute(
            text("""
                SELECT p.id, p.name, p.status, p.region, p.db_schema,
                       p.mongo_database, p.created_at,
                       o.name AS organization_name
                FROM projects p
                LEFT JOIN organizations o ON o.id = p.organization_id
                ORDER BY p.created_at DESC
            """),
        )
    projects = [dict(r) for r in result.mappings()]
    return {"data": projects}


@router.get("/projects/{project_id}", dependencies=[InternalGuard])
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT p.id, p.name, p.slug, p.status, p.region,
                   p.db_schema, p.mongo_database, p.created_at,
                   o.id AS organization_id, o.name AS organization_name
            FROM projects p
            LEFT JOIN organizations o ON o.id = p.organization_id
            WHERE p.id = :project_id
        """),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"data": dict(row)}


@router.get("/users/{user_id}/projects", dependencies=[InternalGuard])
async def list_user_projects(
    user_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List projects owned by a specific user (via their organizations)."""
    result = await db.execute(
        text("""
            SELECT p.id, p.name, p.status, p.region, p.db_schema,
                   p.mongo_database, p.created_at,
                   o.name AS organization_name
            FROM projects p
            JOIN organizations o ON o.id = p.organization_id
            WHERE o.owner_id = :user_id
            ORDER BY p.created_at DESC
        """),
        {"user_id": user_id},
    )
    projects = [dict(r) for r in result.mappings()]
    print(projects)
    return {"data": projects}


# ─── Platform Auth ────────────────────────────────────────────────────────────

class PlatformSignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str | None = None
    organization_name: str | None = Field(None, alias="organizationName")

    model_config = {"populate_by_name": True}


class PlatformSignInRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/auth/signup", status_code=201, dependencies=[InternalGuard])
async def platform_signup(
    body: PlatformSignUpRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.auth.project_auth import hash_password

    existing = await db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": body.email},
    )
    if existing.first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user_id = str(uuid.uuid4())
    new_org_id = str(uuid.uuid4())
    user_name = body.name or body.email.split("@")[0]
    hashed = hash_password(body.password)

    await db.execute(
        text("""
            INSERT INTO users (id, email, name, hashed_password, is_email_verified)
            VALUES (:id, :email, :name, :pwd, false)
        """),
        {
            "id": user_id,
            "email": body.email,
            "name": user_name,
            "pwd": hashed,
        },
    )

    # Inside your signup route...

#    crte a personal organization for the new user
    await _get_or_create_personal_org(db, user_id, user_name, body.organization_name)
    await db.commit()

    logger.info("Platform user registered: %s", body.email)
    return {
        "data": {
            "user": {
                "id": user_id,
                "email": body.email,
                "name": user_name,
            }
        }
    }


@router.post("/auth/signin", dependencies=[InternalGuard])
async def platform_signin(
    body: PlatformSignInRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from app.auth.project_auth import verify_password

    result = await db.execute(
        text("""
            SELECT id, email, name, hashed_password, is_banned
            FROM users
            WHERE email = :email
        """),
        {"email": body.email},
    )
    row = result.mappings().first()

    if not row:
        # verify_password("dummy", "$2b$12$invalidhashplaceholderxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(body.password, row["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if row["is_banned"]:
        raise HTTPException(status_code=403, detail="This account has been suspended")

    # Ensure personal org exists (for users who signed up before this was added)
    try:
        await _get_or_create_personal_org(db, row["id"], row["name"])
        await db.execute(
            text("UPDATE users SET last_login_at = NOW() WHERE id = :id"),
            {"id": row["id"]},
        )
        await db.commit()
    except Exception as e:
        logger.warning("Failed to update user on signin %s: %s", row["id"], e)
        await db.rollback()

    return {
        "data": {
            "user": {
                "id": row["id"],
                "email": row["email"],
                "name": row["name"],
            }
        }
    }


# ─── Projects ─────────────────────────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    project_id: str
    name: str
    db_schema: str
    mongo_database: str
    region: str = Field(default="lagos")
    description: str | None = None
    # The owner's platform user_id — used to find/create their personal org
    owner_user_id: str


@router.post("/projects", status_code=201, dependencies=[InternalGuard])
async def create_project(
    body: CreateProjectRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Provision all resources for a new project and persist its public record."""
    slug = slugify(body.name)
    jwt_secret = generate_auth_jwt_secret()

    existing = await db.execute(
        text(
            "SELECT id FROM projects WHERE id = :project_id OR slug = :slug OR db_schema = :db_schema OR mongo_database = :mongo_database"
        ),
        {
            "project_id": body.project_id,
            "slug": slug,
            "db_schema": body.db_schema,
            "mongo_database": body.mongo_database,
        },
    )
    if existing.first():
        raise HTTPException(status_code=409, detail="A project with this identifier already exists")

    # Get or create the personal org for this user
    user_result = await db.execute(
        text("SELECT name FROM users WHERE id = :user_id"),
        {"user_id": body.owner_user_id},
    )
    user_row = user_result.mappings().first()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    org_id = await _get_or_create_personal_org(db, body.owner_user_id, user_row["name"])

    try:
        await provision_project_schema(body.project_id, body.db_schema)
        await provision_project_database(body.project_id, body.mongo_database)

        await db.execute(
            text("""
                INSERT INTO projects (
                    id, organization_id, name, slug, region, status,
                    db_schema, mongo_database, auth_jwt_secret
                ) VALUES (
                    :id, :org_id, :name, :slug, :region, 'active',
                    :db_schema, :mongo_database, :auth_jwt_secret
                )
            """),
            {
                "id": body.project_id,
                "org_id": org_id,
                "name": body.name,
                "slug": slug,
                "region": body.region,
                "db_schema": body.db_schema,
                "mongo_database": body.mongo_database,
                "auth_jwt_secret": jwt_secret,
            },
        )
        await db.commit()

        logger.info("Provisioned and saved project: %s (org: %s)", body.project_id, org_id)
        return {"data": {"project_id": body.project_id, "provisioned": True}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Project provisioning failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Infrastructure provisioning failed: {str(e)}")


@router.get("/projects/{project_id}/db-status", dependencies=[InternalGuard])
async def get_db_status(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Check whether the project's PostgreSQL schema and MongoDB database
    have been provisioned. Uses schema existence as the source of truth.
    """
    result = await db.execute(
        text("""
            SELECT p.db_schema, p.mongo_database
            FROM projects p
            WHERE p.id = :project_id
        """),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    # Check if the postgres schema exists
    schema_result = await db.execute(
        text("""
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name = :schema_name
        """),
        {"schema_name": row["db_schema"]},
    )
    schema_exists = schema_result.first() is not None

    return {
        "data": {
            "db_provisioned": schema_exists,
            "db_schema": row["db_schema"],
            "mongo_database": row["mongo_database"],
        }
    }


@router.post("/projects/{project_id}/provision", status_code=201, dependencies=[InternalGuard])
async def provision_project_databases(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually provision the PostgreSQL schema and MongoDB database
    for a project. Idempotent — safe to call multiple times.
    """
    result = await db.execute(
        text("""
            SELECT p.db_schema, p.mongo_database, p.status
            FROM projects p
            WHERE p.id = :project_id
        """),
        {"project_id": project_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    if row["status"] != "active":
        raise HTTPException(status_code=403, detail="Project is not active")

    try:
        await provision_project_schema(project_id, row["db_schema"])
        await provision_project_database(project_id, row["mongo_database"])
        logger.info("Manually provisioned databases for project: %s", project_id)
        return {
            "data": {
                "project_id": project_id,
                "db_schema": row["db_schema"],
                "mongo_database": row["mongo_database"],
                "provisioned": True,
            }
        }
    except Exception as e:
        logger.error("Provision failed for project %s: %s", project_id, e)
        raise HTTPException(status_code=500, detail=f"Provisioning failed: {str(e)}")
    
@router.delete("/projects/{project_id}", dependencies=[InternalGuard])
async def delete_project(
    project_id: str,
    db_schema: str = Query(...),
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    """Teardown all resources for a project."""
    await teardown_project_schema(db_schema)
    await teardown_project_database(mongo_database)
    logger.info("Torn down project: %s", project_id)
    return {"data": {"project_id": project_id, "deleted": True}}


# ─── SQL Tables ───────────────────────────────────────────────────────────────

class CreateTableRequest(BaseModel):
    table: str
    columns: list[dict[str, Any]]
    enable_rls: bool = False
    enable_realtime: bool = False


@router.post("/projects/{project_id}/tables", status_code=201, dependencies=[InternalGuard])
async def create_sql_table(
    project_id: str,
    body: CreateTableRequest,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await create_table(
        db, db_schema, body.table, body.columns,
        enable_rls=body.enable_rls,
        enable_realtime=body.enable_realtime,
    )
    await db.commit()
    return {"data": {"table": body.table, "created": True}}


@router.delete("/projects/{project_id}/tables/{table}", dependencies=[InternalGuard])
async def drop_sql_table(
    project_id: str,
    table: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await drop_table(db, db_schema, table)
    await db.commit()
    return {"data": {"table": table, "dropped": True}}


class AddColumnRequest(BaseModel):
    column: dict[str, Any]


@router.post("/projects/{project_id}/tables/{table}/columns", status_code=201, dependencies=[InternalGuard])
async def add_table_column(
    project_id: str,
    table: str,
    body: AddColumnRequest,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await add_column(db, db_schema, table, body.column)
    await db.commit()
    return {"data": {"column": body.column.get("name"), "added": True}}


@router.delete("/projects/{project_id}/tables/{table}/columns/{column}", dependencies=[InternalGuard])
async def drop_table_column(
    project_id: str,
    table: str,
    column: str,
    db_schema: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await drop_column(db, db_schema, table, column)
    await db.commit()
    return {"data": {"column": column, "dropped": True}}


# ─── NoSQL Collections ────────────────────────────────────────────────────────

class CreateCollectionRequest(BaseModel):
    collection: str
    indexes: list[dict[str, Any]] | None = None
    enable_change_stream: bool = False


@router.post("/projects/{project_id}/collections", status_code=201, dependencies=[InternalGuard])
async def create_nosql_collection(
    project_id: str,
    body: CreateCollectionRequest,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    await create_collection(
        mongo_database,
        body.collection,
        indexes=body.indexes,
        enable_change_stream=body.enable_change_stream,
    )
    return {"data": {"collection": body.collection, "created": True}}


@router.delete("/projects/{project_id}/collections/{collection}", dependencies=[InternalGuard])
async def drop_nosql_collection(
    project_id: str,
    collection: str,
    mongo_database: str = Query(...),
) -> dict[str, Any]:
    await drop_collection(mongo_database, collection)
    return {"data": {"collection": collection, "dropped": True}}


# ─── Storage Buckets ──────────────────────────────────────────────────────────

class CreateBucketRequest(BaseModel):
    bucket: str


@router.post("/projects/{project_id}/buckets", status_code=201, dependencies=[InternalGuard])
async def create_storage_bucket(
    project_id: str,
    body: CreateBucketRequest,
) -> dict[str, Any]:
    full_name = get_bucket_name(project_id, body.bucket)
    ensure_bucket_exists(full_name)
    return {"data": {"bucket": body.bucket, "full_name": full_name, "created": True}}

# ─── Storage (internal dashboard routes) ─────────────────────────────────────
# Add these routes to backend/app/api/internal/router.py
# These routes bypass API key auth and are only accessible via X-Internal-Secret

class PresignUploadRequest(BaseModel):
    filename: str
    content_type: str
    expires_in: int = 3600


@router.get("/storage/{project_id}/{bucket}/files", dependencies=[InternalGuard])
async def internal_list_storage_files(
    project_id: str,
    bucket: str,
    prefix: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """List files in a project's storage bucket — bypasses API key auth for dashboard use."""
    from app.engines.storage_engine import list_files
    files = await list_files(project_id=project_id, bucket=bucket, prefix=prefix or "", limit=limit)
    return {"data": files}


@router.post("/storage/{project_id}/{bucket}/presign-upload", dependencies=[InternalGuard])
async def internal_presign_upload(
    project_id: str,
    bucket: str,
    body: PresignUploadRequest,
) -> dict[str, Any]:
    """Generate a presigned upload URL — for dashboard file uploads."""
    from app.engines.storage_engine import get_presigned_upload_url
    result = await get_presigned_upload_url(
        project_id=project_id,
        bucket=bucket,
        filename=body.filename,
        content_type=body.content_type,
        expires_in=body.expires_in,
    )
    return {"data": result}


@router.delete("/storage/{project_id}/{bucket}/{file_path:path}", dependencies=[InternalGuard])
async def internal_delete_storage_file(
    project_id: str,
    bucket: str,
    file_path: str,
) -> dict[str, Any]:
    """Delete a file from a project's storage bucket — for dashboard use."""
    from app.engines.storage_engine import delete_file
    deleted = await delete_file(project_id=project_id, bucket=bucket, path=file_path)
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    return {"data": {"deleted": True, "key": file_path}}


@router.post("/storage/{project_id}/{bucket}/presign-download", dependencies=[InternalGuard])
async def internal_presign_download(
    project_id: str,
    bucket: str,
    file_key: str = Query(...),
    expires_in: int = Query(default=3600),
) -> dict[str, Any]:
    """Generate a presigned download URL for dashboard use."""
    from app.engines.storage_engine import get_presigned_download_url
    url = await get_presigned_download_url(
        project_id=project_id,
        bucket=bucket,
        file_key=file_key,
        expires_in=expires_in,
    )
    return {"data": {"url": url}}

# ─── API Keys ─────────────────────────────────────────────────────────────────

class CreateApiKeyRequest(BaseModel):
    project_id: str
    key_type: str = Field(default="anon", pattern="^(anon|service)$")
    label: str | None = None


@router.post("/api-keys", status_code=201, dependencies=[InternalGuard])
async def create_api_key(
    body: CreateApiKeyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    import hashlib

    raw_key = f"sk_{body.key_type}_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_id = str(uuid.uuid4())

    await db.execute(
        text("""
            INSERT INTO api_keys (id, project_id, key_hash, key_type, is_active, label)
            VALUES (:id, :project_id, :key_hash, :key_type, true, :label)
        """),
        {
            "id": key_id,
            "project_id": body.project_id,
            "key_hash": key_hash,
            "key_type": body.key_type,
            "label": body.label or f"{body.key_type} key",
        },
    )
    await db.commit()

    return {
        "data": {
            "id": key_id,
            "key": raw_key,
            "key_type": body.key_type,
            "project_id": body.project_id,
        }
    }


@router.delete("/api-keys/{key_id}", dependencies=[InternalGuard])
async def revoke_api_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await db.execute(
        text("UPDATE api_keys SET is_active = false WHERE id = :id"),
        {"id": key_id},
    )
    await db.commit()
    return {"data": {"id": key_id, "revoked": True}}


# ─── Usage ────────────────────────────────────────────────────────────────────

@router.get("/usage/{project_id}", dependencies=[InternalGuard])
async def get_project_usage(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        text("""
            SELECT metric, SUM(value) as total
            FROM usage_records
            WHERE project_id = :project_id
              AND period_start >= NOW() - INTERVAL '30 days'
            GROUP BY metric
        """),
        {"project_id": project_id},
    )
    rows = {r["metric"]: r["total"] for r in result.mappings()}
    return {"data": rows}


# ─── Permissions ──────────────────────────────────────────────────────────────

class UpsertPermissionRequest(BaseModel):
    resource_name: str
    engine: str = Field(pattern="^(sql|nosql|kv)$")
    rules_json: str


@router.put("/projects/{project_id}/permissions", dependencies=[InternalGuard])
async def upsert_permissions(
    project_id: str,
    body: UpsertPermissionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await db.execute(
        text("""
            INSERT INTO resource_permissions (id, project_id, resource_name, engine, rules_json)
            VALUES (:id, :project_id, :resource_name, :engine, :rules_json)
            ON CONFLICT (project_id, resource_name, engine)
            DO UPDATE SET rules_json = EXCLUDED.rules_json
        """),
        {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "resource_name": body.resource_name,
            "engine": body.engine,
            "rules_json": body.rules_json,
        },
    )
    await db.commit()

    from app.db.redis import get_redis
    redis = await get_redis()
    await redis.delete(f"perms:{project_id}:{body.engine}:{body.resource_name}")

    return {"data": {"resource": body.resource_name, "updated": True}}