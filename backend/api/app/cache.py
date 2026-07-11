import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

import redis
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)


class CacheUnavailableError(Exception):
    pass


redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=int(os.getenv("REDIS_CACHE_DB", os.getenv("REDIS_DB", 0))),
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)

REDIS_TTL_SECONDS = int(os.getenv("REDIS_TTL_SECONDS", 86400))
REDIS_OPERATION_TIMEOUT_SECONDS = float(os.getenv("REDIS_OPERATION_TIMEOUT_SECONDS", "2"))
_redis_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="redis-cache")


def _redis_call(func, *args, **kwargs):
    future = _redis_executor.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=REDIS_OPERATION_TIMEOUT_SECONDS)
    except FutureTimeoutError as exc:
        raise CacheUnavailableError("Redis is temporarily unavailable.") from exc


def check_cache_connection():
    try:
        _redis_call(redis_client.ping)
    except redis.RedisError as e:
        logger.warning("Redis health check failed: %s", e)
        raise CacheUnavailableError("Redis is temporarily unavailable.") from e
    return True

def get_cached(key: str):
    try:
        value = _redis_call(redis_client.get, key)
    except (redis.RedisError, CacheUnavailableError) as e:
        logger.warning("Redis cache read failed for %s: %s", key, e)
        return None

    if value is not None:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON in Redis cache key %s", key)
            return None
    return None

def set_cached(key: str, value, ttl: int = REDIS_TTL_SECONDS):
    try:
        _redis_call(redis_client.set, key, json.dumps(value, default=str), ex=ttl)
    except (redis.RedisError, CacheUnavailableError) as e:
        logger.warning("Redis cache write failed for %s: %s", key, e)

def key_exists(key: str):
    """Returns True/False, or None if Redis couldn't be reached (unknown)."""
    try:
        return _redis_call(redis_client.exists, key) == 1
    except (redis.RedisError, CacheUnavailableError) as e:
        logger.warning("Redis existence check failed for %s: %s", key, e)
        return None
