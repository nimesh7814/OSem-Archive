import logging
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from minio import Minio
from minio.commonconfig import ENABLED, Filter
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule
import urllib3

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

MINIO_TTL_SECONDS = int(os.getenv("MINIO_TTL_SECONDS", "86400"))

MEASUREMENT_CACHE_PREFIX = "cache/measurements/"
MEASUREMENT_CACHE_TTL_SECONDS = int(os.getenv("MEASUREMENT_CACHE_TTL_SECONDS", str(14 * 24 * 60 * 60)))
MEASUREMENT_CACHE_TTL_DAYS = max(1, MEASUREMENT_CACHE_TTL_SECONDS // 86400)

EXPORTS_PREFIX = "exports/"
EXPORT_EXPIRE_HOURS = int(os.getenv("EXPORT_EXPIRE_HOURS", "24"))
EXPORT_EXPIRE_DAYS = max(1, -(-EXPORT_EXPIRE_HOURS // 24))

# Set once per process: avoids re-issuing the lifecycle PUT on every single upload.
_lifecycle_policy_confirmed = False


class BucketError(Exception):
    pass


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def minio_client(endpoint=None, secure=None):
    endpoint = endpoint or os.getenv("MINIO_ENDPOINT", "localhost:9000")
    secure = env_bool("MINIO_USE_SSL", False) if secure is None else secure
    access_key = os.getenv("MINIO_ROOT_USER")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD")

    if not access_key or not secret_key:
        raise BucketError("MinIO credentials are not configured.")

    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        region=os.getenv("MINIO_REGION", "us-east-1"),
        http_client=urllib3.PoolManager(
            timeout=urllib3.Timeout(
                connect=float(os.getenv("MINIO_CONNECT_TIMEOUT_SECONDS", "2")),
                read=float(os.getenv("MINIO_READ_TIMEOUT_SECONDS", "2")),
            ),
            retries=False,
        ),
    )


def bucket_name():
    bucket = os.getenv("MINIO_BUCKET")
    if not bucket:
        raise BucketError("MINIO_BUCKET is not configured.")
    return bucket


def ensure_bucket_exists(client, bucket):
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    except Exception as exc:
        raise BucketError(f"Could not prepare MinIO bucket '{bucket}'.") from exc
    _ensure_bucket_lifecycle_policy(client, bucket)


def _ensure_bucket_lifecycle_policy(client, bucket):
    # Isolated on purpose: this is best-effort housekeeping and must never prevent the actual upload/write.
    global _lifecycle_policy_confirmed
    if _lifecycle_policy_confirmed:
        return

    try:
        rules = [
            Rule(
                status=ENABLED,
                rule_id="measurement-cache-expiry",
                rule_filter=Filter(prefix=MEASUREMENT_CACHE_PREFIX),
                expiration=Expiration(days=MEASUREMENT_CACHE_TTL_DAYS),
            ),
            Rule(
                status=ENABLED,
                rule_id="export-file-expiry",
                rule_filter=Filter(prefix=EXPORTS_PREFIX),
                expiration=Expiration(days=EXPORT_EXPIRE_DAYS),
            ),
        ]
        client.set_bucket_lifecycle(bucket, LifecycleConfig(rules))
        _lifecycle_policy_confirmed = True
    except Exception as exc:
        logger.warning("Could not configure MinIO lifecycle policy for bucket '%s': %s", bucket, exc)


def check_object_storage_connection():
    client = minio_client()
    try:
        client.bucket_exists(bucket_name())
    except Exception as exc:
        raise BucketError("Object storage is temporarily unavailable.") from exc
    return True


def upload_file(object_name, file_object, length, content_type):
    client = minio_client()
    bucket = bucket_name()
    ensure_bucket_exists(client, bucket)

    try:
        client.put_object(
            bucket,
            object_name,
            file_object,
            length=length,
            content_type=content_type,
        )
    except Exception as exc:
        raise BucketError("Could not upload export file to MinIO.") from exc

    return {
        "bucket": bucket,
        "objectName": object_name,
        "sizeBytes": length,
        "contentType": content_type,
    }


def get_presigned_download_url(object_name):
    if not object_name:
        raise BucketError("No export object name was provided.")

    check_object_storage_connection()
    # MINIO_ENDPOINT only resolves inside Docker; browser-facing URLs must be signed against a public host.
    public_endpoint = os.getenv("MINIO_PUBLIC_ENDPOINT")
    secure = env_bool("MINIO_PUBLIC_USE_SSL", env_bool("MINIO_USE_SSL", False))
    client = minio_client(endpoint=public_endpoint, secure=secure) if public_endpoint else minio_client()
    expires = timedelta(seconds=MINIO_TTL_SECONDS)

    try:
        return client.presigned_get_object(bucket_name(), object_name, expires=expires)
    except Exception as exc:
        raise BucketError("Could not create a download URL for the export file.") from exc
