import psycopg2.extras

from api.functions.db_con import get_db_connection
from api.functions.redis_cache import cached_or_compute, make_cache_key


def get_stats():
    """Return the precomputed archive summary table."""
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT stations, sensors, readings, countries, summary_date
                    FROM summary
                """)
                return cursor.fetchone()

    key = make_cache_key("stats")
    return cached_or_compute(key, _compute, ttl=300)

