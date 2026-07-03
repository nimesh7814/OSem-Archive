import logging
import os
from dotenv import load_dotenv
import psycopg2
import psycopg2.pool

# Load environment variables from .env file
load_dotenv()

# Constant for database connection
DBNAME = os.getenv("POSTGRES_DB")
USER = os.getenv("POSTGRES_USER")
PASSWORD = os.getenv("POSTGRES_PASSWORD")
HOST = os.getenv("POSTGRES_HOST")
PORT = os.getenv("POSTGRES_PORT")

logger = logging.getLogger(__name__)

# Created once, eagerly, at import time.
db_pool = psycopg2.pool.SimpleConnectionPool(
    minconn=1,
    maxconn=20,
    user=USER,
    password=PASSWORD,
    host=HOST,
    port=PORT,
    database=DBNAME,
)


# Get a connection
def get_connection():
    return db_pool.getconn()


# Release the connection
def release_connection(conn):
    db_pool.putconn(conn)


# Used when a query failed in a way that may have killed the connection server-side.
def discard_connection(conn):
    db_pool.putconn(conn, close=True)


# Execute a query
def execute_query(query, params=None):
    conn = None
    healthy = True

    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(query, params)
        conn.commit()

        result = cur.fetchall() if cur.description else None
        cur.close()

        return result

    except Exception:
        healthy = False
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        logger.exception("Query failed: %s", query)
        raise

    finally:
        if conn:
            if healthy:
                release_connection(conn)
            else:
                discard_connection(conn)