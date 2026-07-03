import os
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = 24 * 60 * 60

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def get_cached_body(key):
    return redis_client.get(key)


def set_cached_body(key, body):
    redis_client.setex(key, CACHE_TTL_SECONDS, body)
