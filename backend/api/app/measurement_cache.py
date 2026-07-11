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


def _is_well_formed_payload(payload):
    # Guards against ever serving a corrupted/truncated cache entry as if it were a real response.
    return isinstance(payload, dict) and isinstance(payload.get("boxes"), list) and "aggregate" in payload


def get_cached_measurements(cache_key):
    try:
        client = minio_client()
        bucket = bucket_name()
        response = client.get_object(bucket, cache_key)
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

    payload = cached.get("payload")
    if not _is_well_formed_payload(payload):
        logger.warning("Ignoring malformed measurement cache entry %s.", cache_key)
        _remove_cache_object(client, bucket, cache_key)
        return None

    return payload


def _remove_cache_object(client, bucket, cache_key):
    try:
        client.remove_object(bucket, cache_key)
    except Exception as exc:
        logger.warning("Could not remove corrupted measurement cache entry %s: %s", cache_key, exc)


def set_cached_measurements(cache_key, payload):
    try:
        client = minio_client()
        bucket = bucket_name()
        ensure_bucket_exists(client, bucket)
        _ensure_cache_lifecycle_policy(client, bucket)

        body = json.dumps({"cachedAt": time.time(), "payload": payload}, default=str).encode("utf-8")
        expected_md5 = hashlib.md5(body).hexdigest()

        result = client.put_object(bucket, cache_key, io.BytesIO(body), length=len(body), content_type="application/json")

        # A dash means MinIO used multipart upload (own checksums apply); only compare plain single-part MD5 etags.
        actual_etag = (result.etag or "").strip('"')
        if "-" not in actual_etag and actual_etag != expected_md5:
            logger.warning("Measurement cache entry %s failed integrity check after write (etag mismatch); removing.", cache_key)
            _remove_cache_object(client, bucket, cache_key)
    except Exception as exc:
        # Caching is a performance optimization; a write failure must never break the response.
        logger.warning("Could not write measurement cache entry %s: %s", cache_key, exc)
