import hashlib
import io
import json
import logging
import os
import time

from minio.commonconfig import ENABLED, Filter
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

try:
    from .bucket import bucket_name, ensure_bucket_exists, minio_client
except ImportError:
    from bucket import bucket_name, ensure_bucket_exists, minio_client

logger = logging.getLogger(__name__)

MEASUREMENT_CACHE_PREFIX = "cache/measurements/"
MEASUREMENT_CACHE_TTL_SECONDS = int(os.getenv("MEASUREMENT_CACHE_TTL_SECONDS", str(14 * 24 * 60 * 60)))
MEASUREMENT_CACHE_TTL_DAYS = max(1, MEASUREMENT_CACHE_TTL_SECONDS // 86400)

# Set once per process: avoids re-issuing the lifecycle PUT on every single cache write.
_lifecycle_policy_confirmed = False


def _ensure_cache_lifecycle_policy(client, bucket):
    # Isolated on purpose: this is best-effort housekeeping and must never prevent the actual cache write below.
    global _lifecycle_policy_confirmed
    if _lifecycle_policy_confirmed:
        return

    try:
        rule = Rule(
            status=ENABLED,
            rule_id="measurement-cache-expiry",
            rule_filter=Filter(prefix=MEASUREMENT_CACHE_PREFIX),
            expiration=Expiration(days=MEASUREMENT_CACHE_TTL_DAYS),
        )
        client.set_bucket_lifecycle(bucket, LifecycleConfig([rule]))
        _lifecycle_policy_confirmed = True
    except Exception as exc:
        logger.warning("Could not configure MinIO lifecycle policy for measurement cache: %s", exc)


def _fingerprint(*parts):
    canonical = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _filter_key_parts(filters, tag_filter, phenomenon_filter, exposure_filter):
    return (
        filters.from_date.isoformat(),
        filters.to_date.isoformat(),
        filters.aggregate,
        sorted(tag_filter) if tag_filter else None,
        sorted(phenomenon_filter) if phenomenon_filter else None,
        sorted(exposure_filter) if exposure_filter else None,
    )


def region_cache_key(country, region, filters, tag_filter, phenomenon_filter, exposure_filter):
    digest = _fingerprint(country, region, *_filter_key_parts(filters, tag_filter, phenomenon_filter, exposure_filter))
    return f"{MEASUREMENT_CACHE_PREFIX}region/{digest}.json"


def aoi_cache_key(geometry, filters, tag_filter, phenomenon_filter, exposure_filter):
    digest = _fingerprint(geometry, *_filter_key_parts(filters, tag_filter, phenomenon_filter, exposure_filter))
    return f"{MEASUREMENT_CACHE_PREFIX}aoi/{digest}.json"


def get_cached_measurements(cache_key):
    try:
        client = minio_client()
        response = client.get_object(bucket_name(), cache_key)
        try:
            raw = response.read()
        finally:
            response.close()
            response.release_conn()
        cached = json.loads(raw)
    except Exception:
        return None

    cached_at = cached.get("cachedAt")
    if not isinstance(cached_at, (int, float)) or (time.time() - cached_at) > MEASUREMENT_CACHE_TTL_SECONDS:
        return None

    return cached.get("payload")


def set_cached_measurements(cache_key, payload):
    try:
        client = minio_client()
        bucket = bucket_name()
        ensure_bucket_exists(client, bucket)
        _ensure_cache_lifecycle_policy(client, bucket)

        body = json.dumps({"cachedAt": time.time(), "payload": payload}, default=str).encode("utf-8")
        client.put_object(bucket, cache_key, io.BytesIO(body), length=len(body), content_type="application/json")
    except Exception as exc:
        # Caching is a performance optimization; a write failure must never break the response.
        logger.warning("Could not write measurement cache entry %s: %s", cache_key, exc)
