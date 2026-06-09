# backend/app/engines/storage_engine.py
import logging
import uuid
import asyncio
from typing import Any

from botocore.exceptions import ClientError
from botocore.client import Config
import boto3
from app.config import settings
from app.storage.minio import ensure_bucket_exists, get_bucket_name, get_s3_client

logger = logging.getLogger(__name__)

PRESIGNED_URL_EXPIRES_IN = 3600


def _get_public_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.minio_public_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name=settings.region,
        config=Config(signature_version="s3v4"),
    )


def _verify_bucket_ownership(project_id: str, bucket: str) -> str:
    return get_bucket_name(project_id, bucket)


async def ensure_bucket(full_bucket: str) -> None:
    await asyncio.to_thread(ensure_bucket_exists, full_bucket)


async def get_upload_url(project_id: str, bucket: str, filename: str, content_type: str) -> str:
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()
    def _generate() -> str:
        ensure_bucket_exists(safe_bucket)
        return s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": safe_bucket, "Key": filename, "ContentType": content_type},
            ExpiresIn=PRESIGNED_URL_EXPIRES_IN,
        )
    return await asyncio.to_thread(_generate)


async def get_presigned_upload_url(
    project_id: str, bucket: str, filename: str, content_type: str, expires_in: int = 3600,
) -> dict[str, str]:
    full_bucket = _verify_bucket_ownership(project_id, bucket)
    key = f"{uuid.uuid4().hex}/{filename}"
    def _generate() -> dict[str, str]:
        ensure_bucket_exists(full_bucket)
        s3_public = _get_public_s3_client()
        upload_url = s3_public.generate_presigned_url(
            "put_object",
            Params={"Bucket": full_bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )
        public_endpoint = settings.minio_public_endpoint.rstrip("/")
        internal = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint.rstrip('/')}"
        if upload_url.startswith(internal):
            upload_url = upload_url.replace(internal, public_endpoint, 1)
        file_url = f"{public_endpoint}/{full_bucket}/{key}"
        return {"upload_url": upload_url, "file_url": file_url, "key": key, "expires_in": str(expires_in)}
    return await asyncio.to_thread(_generate)


async def get_download_url(project_id: str, bucket: str, path: str) -> str:
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()
    def _generate() -> str:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": safe_bucket, "Key": path},
            ExpiresIn=PRESIGNED_URL_EXPIRES_IN,
        )
    return await asyncio.to_thread(_generate)


async def get_presigned_download_url(project_id: str, bucket: str, file_key: str, expires_in: int = 3600) -> str:
    full_bucket = _verify_bucket_ownership(project_id, bucket)
    def _generate() -> str:
        s3_public = _get_public_s3_client()
        return s3_public.generate_presigned_url(
            "get_object",
            Params={"Bucket": full_bucket, "Key": file_key},
            ExpiresIn=expires_in,
        )
    return await asyncio.to_thread(_generate)


async def list_files(project_id: str, bucket: str, prefix: str = "", limit: int = 200) -> list[dict[str, Any]]:
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()
    def _list() -> list[dict[str, Any]]:
        try:
            kwargs: dict[str, Any] = {"Bucket": safe_bucket, "MaxKeys": min(limit, 1000)}
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


async def delete_file(project_id: str, bucket: str, path: str = "", file_key: str = "") -> bool:
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


# ─── Delete bucket ────────────────────────────────────────────────────────────

def _purge_bucket_sync(s3, bucket_name: str) -> None:
    """
    Remove every object, object version, and delete marker from a bucket.

    Three passes so the bucket is truly empty regardless of versioning state:
      1. list_objects_v2     — current objects (versioning off / suspended)
      2. list_object_versions (Versions)      — all stored versions
      3. list_object_versions (DeleteMarkers) — tombstone markers

    Each item is deleted one-by-one (no DeleteObjects batch) so this works
    correctly on Backblaze B2 which does not support the batch API.
    """
    # Pass 1 — current objects
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            for item in page.get("Contents", []):
                try:
                    s3.delete_object(Bucket=bucket_name, Key=item["Key"])
                    logger.debug("Deleted object %s/%s", bucket_name, item["Key"])
                except ClientError as exc:
                    logger.warning("Could not delete %s/%s: %s", bucket_name, item["Key"], exc)
    except ClientError as exc:
        code = exc.response["Error"].get("Code", "")
        if code not in ("NoSuchBucket", "404"):
            logger.warning("list_objects_v2 failed for %s: %s", bucket_name, exc)

    # Passes 2 & 3 — versioned objects and delete markers
    # Providers that don't support versioning return MethodNotAllowed / NotImplemented — safe to skip.
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket_name):
            for version in page.get("Versions", []):
                try:
                    s3.delete_object(Bucket=bucket_name, Key=version["Key"], VersionId=version["VersionId"])
                    logger.debug("Deleted version %s/%s@%s", bucket_name, version["Key"], version["VersionId"])
                except ClientError as exc:
                    logger.warning("Could not delete version %s/%s@%s: %s", bucket_name, version["Key"], version["VersionId"], exc)

            for marker in page.get("DeleteMarkers", []):
                try:
                    s3.delete_object(Bucket=bucket_name, Key=marker["Key"], VersionId=marker["VersionId"])
                    logger.debug("Deleted marker %s/%s@%s", bucket_name, marker["Key"], marker["VersionId"])
                except ClientError as exc:
                    logger.warning("Could not delete marker %s/%s@%s: %s", bucket_name, marker["Key"], marker["VersionId"], exc)
    except ClientError as exc:
        code = exc.response["Error"].get("Code", "")
        if code not in ("NoSuchBucket", "404", "MethodNotAllowed", "NotImplemented"):
            logger.warning("list_object_versions failed for %s: %s", bucket_name, exc)


async def delete_bucket(project_id: str, bucket: str) -> bool:
    """
    Delete a project's storage bucket and all its contents.

    Force-deletes even if the bucket is non-empty. Works on MinIO and
    Backblaze B2. Purges objects → versions → delete markers before
    calling the actual bucket delete.
    """
    safe_bucket = get_bucket_name(project_id, bucket)
    s3 = get_s3_client()

    def _delete() -> bool:
        # Verify bucket exists
        try:
            s3.head_bucket(Bucket=safe_bucket)
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            if code in ("404", "NoSuchBucket", "403", "AccessDenied"):
                logger.info("Bucket %s not found — nothing to delete", safe_bucket)
                return False
            raise

        logger.info("Purging all contents of bucket %s …", safe_bucket)
        _purge_bucket_sync(s3, safe_bucket)

        # Now delete the (empty) bucket
        try:
            s3.delete_bucket(Bucket=safe_bucket)
            logger.info("Bucket %s deleted successfully", safe_bucket)
            return True
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            if code in ("NoSuchBucket", "404"):
                return True  # already gone — treat as success
            logger.error("delete_bucket %s failed after purge: %s", safe_bucket, exc)
            raise

    return await asyncio.to_thread(_delete)


async def get_bucket_stats(project_id: str, bucket: str) -> dict[str, Any]:
    files = await list_files(project_id, bucket, limit=1000)
    total_size = sum(f["size"] for f in files)
    return {"file_count": len(files), "total_size": total_size, "bucket": bucket}