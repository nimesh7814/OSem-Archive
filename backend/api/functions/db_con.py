from contextlib import contextmanager

from psycopg2.pool import SimpleConnectionPool

from api.functions.config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER


db_pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
)


@contextmanager
def get_db_connection():
    """Borrow a Postgres connection from the shared process-local pool."""
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)


def close_pool():
    db_pool.closeall()

