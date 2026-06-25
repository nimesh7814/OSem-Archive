from .db_con import get_db_connection


async def list_phenomena() -> list[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT title, sensor_type FROM sensors ORDER BY title")
            return [{"title": r[0], "sensorType": r[1]} for r in cur.fetchall()]
