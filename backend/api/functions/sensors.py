import psycopg2.extras

from api.functions.db_con import get_db_connection
from api.functions.redis_cache import cached_or_compute, make_cache_key


def list_sensors():
    """Return values used by frontend sensor filters."""
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""SELECT DISTINCT title FROM sensors ORDER BY title;""")
                titles = [row["title"] for row in cursor.fetchall()]

                cursor.execute("""SELECT DISTINCT sensor_type FROM sensors ORDER BY sensor_type;""")
                sensor_types = [row["sensor_type"] for row in cursor.fetchall()]

                cursor.execute("""SELECT DISTINCT unit FROM sensors ORDER BY unit;""")
                units = [row["unit"] for row in cursor.fetchall()]

        return {"tags": titles, "phenomenon": sensor_types, "units": units}

    key = make_cache_key("sensors")
    return cached_or_compute(key, _compute, ttl=3600)


def list_phenomena():
    """Compatibility response for the designed frontend sensor selector."""
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DISTINCT title
                    FROM sensors
                    WHERE title IS NOT NULL
                    ORDER BY title;
                """)
                return [{"title": row["title"]} for row in cursor.fetchall()]

    key = make_cache_key("phenomena")
    return cached_or_compute(key, _compute, ttl=3600)


def list_exposures():
    """Return distinct exposure values for archive filters."""
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DISTINCT exposure
                    FROM boxes
                    WHERE exposure IS NOT NULL
                    ORDER BY exposure;
                """)
                return [row["exposure"] for row in cursor.fetchall()]

    key = make_cache_key("exposures")
    return cached_or_compute(key, _compute, ttl=3600)
