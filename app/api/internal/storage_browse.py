# backend/app/api/internal/storage_browse.py
"""
Internal-only storage browse endpoints for the dashboard.
Bypasses API key auth — only callable via X-Internal-Secret.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.tasks.usage_sync import record_usage

router = APIRouter(tags=["Internal Storage Browse"])
logger = logging.getLogger(__name__)


async def require_internal(x_internal_secret: str = Header(...)) -> None:
    if x_internal_secret != settings.internal_api_secret:
        raise HTTPException(status_code=401, detail="Invalid internal secret")


InternalGuard = Depends(require_internal)


@router.get("/storage/{project_id}/buckets", dependencies=[InternalGuard])
async def list_project_buckets(
    project_id: str,
    include_stats: bool = Query(default=False),
) -> dict[str, Any]:
    """
    List available buckets for a project.
    Buckets are discovered by scanning for prefix {project_id}-.
    """
    import asyncio
    from app.storage.minio import get_s3_client

    s3 = get_s3_client()

    def _list_buckets():
        try:
            response = s3.list_buckets()
            all_buckets = response.get("Buckets", [])
            safe_project = project_id.lower().replace("_", "-")
            prefix = f"{safe_project}-"
            return [
                b["Name"].removeprefix(prefix)
                for b in all_buckets
                if b["Name"].startswith(prefix)
            ]
        except Exception as e:
            logger.warning("Failed to list buckets: %s", e)
            return []

    project_bucket_names = await asyncio.to_thread(_list_buckets)

    buckets = []
    for bucket_name in project_bucket_names:
        info: dict[str, Any] = {"name": bucket_name}
        if include_stats:
            try:
                from app.engines.storage_engine import get_bucket_stats
                stats = await get_bucket_stats(project_id, bucket_name)
                info.update(stats)
            except Exception:
                info.update({"file_count": 0, "total_size": 0})
        buckets.append(info)

    return {"data": {"buckets": buckets}}


@router.post("/storage/{project_id}/buckets", status_code=201, dependencies=[InternalGuard])
async def create_bucket(
    project_id: str,
    bucket: str = Query(...),
) -> dict[str, Any]:
    """Create a new storage bucket for the project."""
    import asyncio
    from app.storage.minio import ensure_bucket_exists, get_bucket_name

    full_name = get_bucket_name(project_id, bucket)

    try:
        await asyncio.to_thread(ensure_bucket_exists, full_name)
        return {"data": {"bucket": bucket, "full_name": full_name, "created": True}}
    except Exception as e:
        logger.error("Failed to create bucket %s: %s", full_name, e)
        raise HTTPException(status_code=500, detail=f"Could not create bucket: {e}")


@router.delete("/storage/{project_id}/buckets/{bucket}", dependencies=[InternalGuard])
async def delete_bucket(
    project_id: str,
    bucket: str,
) -> dict[str, Any]:
    """Delete a storage bucket for the project."""
    from app.engines.storage_engine import delete_bucket

    deleted = await delete_bucket(project_id=project_id, bucket=bucket)
    if not deleted:
        raise HTTPException(status_code=404, detail="Bucket not found or could not be deleted")
    return {"data": {"deleted": True, "bucket": bucket}}


@router.get("/storage/{project_id}/{bucket}/files", dependencies=[InternalGuard])
async def list_storage_files(
    project_id: str,
    bucket: str,
    prefix: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """List files in a project's storage bucket."""
    from app.engines.storage_engine import list_files
    try:
        files = await list_files(
            project_id=project_id,
            bucket=bucket,
            prefix=prefix or "",
            limit=limit,
        )
        return {"data": files}
    except Exception as e:
        logger.error("Failed to list files for %s/%s: %s", project_id, bucket, e)
        return {"data": []}


@router.post("/storage/{project_id}/{bucket}/presign-upload", dependencies=[InternalGuard])
async def presign_upload(
    project_id: str,
    bucket: str,
    body: "PresignUploadRequest",
) -> dict[str, Any]:
    """Generate a presigned upload URL for the dashboard."""
    from app.engines.storage_engine import get_presigned_upload_url
    print(body)
    try:
        result = await get_presigned_upload_url(
            project_id=project_id,
            bucket=bucket,
            filename=body.filename,
            content_type=body.content_type,
            expires_in=body.expires_in,
        )
        record_usage.delay(project_id, "storage_bytes", 1)
        return {"data": result}
    except Exception as e:
        logger.error("Failed to presign upload for %s/%s: %s", project_id, bucket, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/storage/{project_id}/{bucket}/presign-download", dependencies=[InternalGuard])
async def presign_download(
    project_id: str,
    bucket: str,
    file_key: str = Query(...),
    expires_in: int = Query(default=3600, ge=60, le=86400),
) -> dict[str, Any]:
    """Generate a presigned download URL."""
    from app.engines.storage_engine import get_presigned_download_url
    try:
        url = await get_presigned_download_url(
            project_id=project_id,
            bucket=bucket,
            file_key=file_key,
            expires_in=expires_in,
        )
        return {"data": {"url": url, "key": file_key, "expires_in": expires_in}}
    except Exception as e:
        logger.error("Failed to presign download for %s/%s/%s: %s", project_id, bucket, file_key, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/storage/{project_id}/{bucket}/{file_path:path}", dependencies=[InternalGuard])
async def delete_storage_file(
    project_id: str,
    bucket: str,
    file_path: str,
) -> dict[str, Any]:
    """Delete a file from storage."""
    from app.engines.storage_engine import delete_file
    deleted = await delete_file(project_id=project_id, bucket=bucket, path=file_path)
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found or already deleted")
    return {"data": {"deleted": True, "key": file_path}}


@router.get("/storage/{project_id}/{bucket}/stats", dependencies=[InternalGuard])
async def get_storage_stats(
    project_id: str,
    bucket: str,
) -> dict[str, Any]:
    """Get aggregate stats for a bucket."""
    from app.engines.storage_engine import get_bucket_stats
    try:
        stats = await get_bucket_stats(project_id, bucket)
        return {"data": stats}
    except Exception as e:
        logger.error("Failed to get stats for %s/%s: %s", project_id, bucket, e)
        return {"data": {"file_count": 0, "total_size": 0, "bucket": bucket}}


class PresignUploadRequest(BaseModel):
    filename: str
    content_type: str
    expires_in: int = 3600