import boto3
from botocore.exceptions import ClientError

from app.config import settings

_s3_client = None



def get_s3_client():  # type: ignore[no-untyped-def]
    global _s3_client
    if _s3_client is None:
        protocol = "https" if settings.minio_use_ssl else "http"
        _s3_client = boto3.client(
            "s3",
            endpoint_url=f"{protocol}://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name="us-east-1",
        )
    return _s3_client


def ensure_bucket_exists(bucket_name: str) -> None:
    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=bucket_name)
    except ClientError as e:
        error_code = int(e.response["Error"]["Code"])
        if error_code == 404:
            s3.create_bucket(Bucket=bucket_name)
        else:
            raise


def get_bucket_name(project_id: str, user_bucket: str) -> str:
    """Construct a safe bucket name: {projectId}-{userBucketName}"""
    safe_project = project_id.lower().replace("_", "-")
    safe_bucket = user_bucket.lower().replace("_", "-")
    return f"{safe_project}-{safe_bucket}"


__all__ = ["get_s3_client", "ensure_bucket_exists", "get_bucket_name", "ClientError"]