# backend/app/engines/storage_engine.py
import logging
import uuid
import asyncio
from typing import Any

from botocore.exceptions import ClientError

from app.config import settings
from app.storage.minio import get_bucket_name, get_s3_client

logger = logging.getLogger(__name__)

PRESIGNED_URL_EXPIRES_IN = 3600


def _verify_bucket_ownership(project_id: str, bucket: str) -> str:
    """Construct and verify the full bucket name belongs to the project."""
    return get_bucket_name(project_id, bucket)


# ─── Bucket helpers ───────────────────────────────────────────────────────────

async def ensure_bucket(full_bucket: str) -> None:
    """Create bucket if it does not exist. Runs boto3 in a thread."""
    s3 = get_s3_client()

    def _ensure() -> None:
        try:
            s3.head_bucket(Bucket=full_bucket)
        except ClientError as e:
            code = int(e.response["Error"]["Code"])
            if code == 404:
                s3.create_bucket(Bucket=full_bucket)
            else:
                raise

    await asyncio.to_thread(_ensure)


# ─── Upload URL ───────────────────────────────────────────────────────────────

async def get_upload_url(
    project_id: str,
    bucket: str,
    filename: str,
    content_type: str,
) -> str:
    """Generate a presigned PUT URL for direct client uploads."""
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()

    def _generate() -> str:
        try:
            s3.head_bucket(Bucket=safe_bucket)
        except ClientError as e:
            if int(e.response["Error"]["Code"]) == 404:
                s3.create_bucket(Bucket=safe_bucket)
            else:
                raise
        return s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": safe_bucket, "Key": filename, "ContentType": content_type},
            ExpiresIn=PRESIGNED_URL_EXPIRES_IN,
        )

    return await asyncio.to_thread(_generate)


async def get_presigned_upload_url(
    project_id: str,
    bucket: str,
    filename: str,
    content_type: str,
    expires_in: int = 3600,
) -> dict[str, str]:
    """Generate a presigned upload URL and return upload + download URLs."""
    full_bucket = _verify_bucket_ownership(project_id, bucket)
    s3 = get_s3_client()
    key = f"{uuid.uuid4().hex}/{filename}"

    def _generate() -> dict[str, str]:
        # Ensure bucket exists
        try:
            s3.head_bucket(Bucket=full_bucket)
        except ClientError as e:
            if int(e.response["Error"]["Code"]) == 404:
                s3.create_bucket(Bucket=full_bucket)
            else:
                raise

        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": full_bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )

        # Replace internal Docker hostname with the public-facing endpoint
        if settings.minio_endpoint in upload_url:
            public_endpoint = settings.minio_public_endpoint.rstrip("/")
            internal = f"http://{settings.minio_endpoint}"
            upload_url = upload_url.replace(internal, public_endpoint)

        file_url = f"{settings.minio_public_endpoint.rstrip('/')}/{full_bucket}/{key}"

        return {
            "upload_url": upload_url,
            "file_url": file_url,
            "key": key,
            "expires_in": str(expires_in),
        }

    return await asyncio.to_thread(_generate)


# ─── Download URL ─────────────────────────────────────────────────────────────

async def get_download_url(
    project_id: str,
    bucket: str,
    path: str,
) -> str:
    """Generate a presigned GET URL for direct client downloads."""
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()

    def _generate() -> str:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": safe_bucket, "Key": path},
            ExpiresIn=PRESIGNED_URL_EXPIRES_IN,
        )

    return await asyncio.to_thread(_generate)


async def get_presigned_download_url(
    project_id: str,
    bucket: str,
    file_key: str,
    expires_in: int = 3600,
) -> str:
    """Generate a presigned GET URL with public endpoint correction."""
    full_bucket = _verify_bucket_ownership(project_id, bucket)
    s3 = get_s3_client()

    def _generate() -> str:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": full_bucket, "Key": file_key},
            ExpiresIn=expires_in,
        )
        if settings.minio_endpoint in url:
            public_endpoint = settings.minio_public_endpoint.rstrip("/")
            internal = f"http://{settings.minio_endpoint}"
            url = url.replace(internal, public_endpoint)
        return url

    return await asyncio.to_thread(_generate)


# ─── List files ───────────────────────────────────────────────────────────────

async def list_files(
    project_id: str,
    bucket: str,
    prefix: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List all files in a project's bucket, optionally filtered by prefix."""
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()

    def _list() -> list[dict[str, Any]]:
        try:
            kwargs: dict[str, Any] = {
                "Bucket": safe_bucket,
                "MaxKeys": min(limit, 1000),
            }
            if prefix:
                kwargs["Prefix"] = prefix

            response = s3.list_objects_v2(**kwargs)
            if "Contents" not in response:
                return []

            return [
                {
                    "key": item["Key"],
                    "size": item["Size"],
                    "last_modified": item["LastModified"].isoformat(),
                    "etag": item["ETag"].strip('"'),
                    "content_type": item.get("ContentType", ""),
                }
                for item in response["Contents"]
            ]
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("404", "NoSuchBucket"):
                return []
            raise

    return await asyncio.to_thread(_list)


# ─── Delete ───────────────────────────────────────────────────────────────────

async def delete_file(
    project_id: str,
    bucket: str,
    path: str = "",
    file_key: str = "",
) -> bool:
    """Delete a file from a project's bucket. Accepts `path` or `file_key`."""
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()
    key = file_key or path

    def _delete() -> bool:
        try:
            s3.delete_object(Bucket=safe_bucket, Key=key)
            return True
        except ClientError as e:
            logger.error("Failed to delete object %s: %s", key, e)
            return False

    return await asyncio.to_thread(_delete)


# ─── Bucket stats ─────────────────────────────────────────────────────────────

async def get_bucket_stats(project_id: str, bucket: str) -> dict[str, Any]:
    """Return aggregate stats (total files, total size) for a bucket."""
    files = await list_files(project_id, bucket, limit=1000)
    total_size = sum(f["size"] for f in files)
    return {
        "file_count": len(files),
        "total_size": total_size,
        "bucket": bucket,
    }