import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)
_s3_client = None


def get_storage_cors_origins() -> list[str]:
    origins = [origin.strip() for origin in settings.storage_cors_allowed_origins.split(",") if origin.strip()]
    return origins or ["*"]


def put_bucket_cors(bucket_name: str) -> None:
    s3 = get_s3_client()
    cors_rules = [
        {
            "AllowedOrigins": get_storage_cors_origins(),
            "AllowedMethods": ["GET", "HEAD", "PUT", "POST", "DELETE"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 300,
        }
    ]

    try:
        s3.put_bucket_cors(Bucket=bucket_name, CORSConfiguration={"CORSRules": cors_rules})
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        error_message = e.response.get("Error", {}).get("Message", "")

        if error_code == "InvalidRequest" and "B2 Native CORS rules" in error_message:
            logger.warning(
                "Bucket %s contains Backblaze B2 native CORS rules; skipping S3 PutBucketCors.",
                bucket_name,
            )
            return

        raise


def get_s3_client():  # type: ignore[no-untyped-def]
    global _s3_client
    if _s3_client is None:
        protocol = "https" if settings.minio_use_ssl else "http"
        _s3_client = boto3.client(
            "s3",
            endpoint_url=f"{protocol}://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name=settings.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )
        # For Backblaze B2, ensure `settings.region` is the B2 region
        # (for example: us-west-002) so signature generation matches.
    return _s3_client


def ensure_bucket_exists(bucket_name: str) -> None:
    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=bucket_name)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in ("404", "NoSuchBucket", "403", "AccessDenied"):
            s3.create_bucket(Bucket=bucket_name)
        else:
            raise
    put_bucket_cors(bucket_name)


def get_bucket_name(project_id: str, user_bucket: str) -> str:
    """Construct a safe bucket name: {projectId}-{userBucketName}"""
    safe_project = project_id.lower().replace("_", "-")
    safe_bucket = user_bucket.lower().replace("_", "-")
    return f"{safe_project}-{safe_bucket}"


__all__ = ["get_s3_client", "ensure_bucket_exists", "get_bucket_name", "ClientError"]