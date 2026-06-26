from .db_con import get_db_connection


async def get_stats() -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM boxes) AS total_stations,
                    (SELECT COUNT(*) FROM sensors) AS total_sensors,
                    (SELECT COUNT(*) FROM measurements) AS total_readings,
                    (
                        SELECT COUNT(DISTINCT r.country)
                        FROM boxes b
                        INNER JOIN regions r ON r.id = b.region_id
                    ) AS total_countries
                """
            )
            total_stations, total_sensors, total_readings, total_countries = cur.fetchone()

    return {
        "total_stations": total_stations,
        "total_sensors": total_sensors,
        "total_readings": total_readings,
        "total_countries": total_countries,
    }
