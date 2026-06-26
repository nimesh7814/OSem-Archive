from .db_con import get_db_connection


async def list_exposures() -> list[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT exposure
                FROM boxes
                WHERE exposure IS NOT NULL
                ORDER BY exposure
                """
            )
            return [row[0] for row in cur.fetchall()]
