import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

import redis

from api.functions.config import REDIS_URL


redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def make_cache_key(prefix: str, **parts) -> str:
    """Build stable Redis keys from filter dictionaries."""
    raw = json.dumps(parts, sort_keys=True, default=_json_default)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{prefix}:{digest}"


def get_cached_json(key: str):
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)
    return None


def set_cached_json(key: str, value, ttl: Optional[int] = None):
    payload = json.dumps(value, default=_json_default)
    if ttl is None:
        redis_client.set(key, payload)
    else:
        redis_client.set(key, payload, ex=ttl)


def cached_or_compute(key: str, compute_fn, ttl: int = 300):
    # Check the raw cache hit, not the deserialized value: a cached `None`
    # (e.g. a "not found" result) round-trips through JSON as "null", which
    # is indistinguishable from a cache miss if we deserialize first.
    cached = redis_client.get(key)
    if cached is not None:
        return json.loads(cached)

    result = compute_fn()
    set_cached_json(key, result, ttl=ttl)
    return result

