import psycopg2.extras
from fastapi import HTTPException

from api.functions.db_con import get_db_connection
from api.functions.redis_cache import cached_or_compute, make_cache_key


def list_countries():
    """Return countries grouped with their available regions."""
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DISTINCT r.country, r.region
                    FROM regions r
                    JOIN boxes b ON r.id = b.region_id
                    ORDER BY r.country, r.region;
                """)
                rows = cursor.fetchall()

        countries = {}
        for row in rows:
            country = row["country"]
            if country not in countries:
                countries[country] = {"country": country, "regions": []}
            countries[country]["regions"].append(row["region"])

        return list(countries.values())

    key = make_cache_key("countries")
    return cached_or_compute(key, _compute, ttl=3600)


def list_regions(country: str):
    """Return regions for one country, or 404 when the country is unknown."""
    def _compute():
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DISTINCT r.country, r.region
                    FROM regions r
                    JOIN boxes b ON r.id = b.region_id
                    WHERE r.country = %s
                    ORDER BY r.region;
                """, (country,))
                rows = cursor.fetchall()

        if not rows:
            return None

        return {
            "country": rows[0]["country"],
            "regions": [row["region"] for row in rows],
        }

    key = make_cache_key("country_regions", country=country)
    result = cached_or_compute(key, _compute, ttl=3600)

    if result is None:
        raise HTTPException(status_code=404, detail="Country not found")

    return result


def list_regions_catalog(country: str | None = None):
    """Compatibility response for the designed frontend country/region picker."""
    if country:
        return list_regions(country)

    countries = list_countries()
    return {"countries": [{"country": item["country"]} for item in countries]}
