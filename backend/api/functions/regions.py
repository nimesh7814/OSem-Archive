from .db_con import get_db_connection


async def list_regions(country: str | None) -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if country:
                cur.execute(
                    """
                    SELECT r.region
                    FROM boxes b
                    INNER JOIN regions r ON b.region_id = r.id
                    WHERE r.country = %s
                    GROUP BY r.region
                    ORDER BY r.region
                    """,
                    (country,),
                )
                return {"country": country, "regions": [row[0] for row in cur.fetchall()]}

            cur.execute(
                """
                SELECT DISTINCT r.country, r.region
                FROM boxes b
                INNER JOIN regions r ON b.region_id = r.id
                GROUP BY r.country, r.region
                ORDER BY r.country, r.region
                """
            )
            countries: dict[str, list[str]] = {}
            for country_name, region_name in cur.fetchall():
                countries.setdefault(country_name, []).append(region_name)
            return {
                "countries": [{"country": c, "regions": rs} for c, rs in countries.items()]
            }
