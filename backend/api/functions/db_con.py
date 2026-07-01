from contextlib import contextmanager

from psycopg2.pool import ThreadedConnectionPool

from api.functions.config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER


# FastAPI runs sync `def` routes in a worker thread pool, so concurrent
# requests can call getconn()/putconn() from different threads at once.
# SimpleConnectionPool has no internal locking for that; ThreadedConnectionPool does.
db_pool = ThreadedConnectionPool(
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

