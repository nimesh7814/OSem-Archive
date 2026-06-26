"""
api/functions/db_con.py

psycopg2 connection pool shared by all route modules. A single pool is
created once per process and reused across requests.
"""

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from psycopg2 import pool

env_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=env_path)

DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")
DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")

POOL_MIN_CONN = int(os.getenv("DB_POOL_MIN_CONN", "2"))
POOL_MAX_CONN = int(os.getenv("DB_POOL_MAX_CONN", "10"))

_pool: pool.ThreadedConnectionPool | None = None


def _validate_db_config() -> None:
    required = {
        "POSTGRES_DB": DB_NAME,
        "POSTGRES_USER": DB_USER,
        "POSTGRES_PASSWORD": DB_PASSWORD,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Missing database environment variable(s): {joined}. "
            f"Set them in the environment or in {env_path}."
        )


def _get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _validate_db_config()
        _pool = pool.ThreadedConnectionPool(
            POOL_MIN_CONN,
            POOL_MAX_CONN,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )
    return _pool


@contextmanager
def get_db_connection():
    """
    Usage:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)

    Returns the connection to the pool on exit instead of closing the
    underlying TCP connection — that's the whole point of pooling.
    """
    conn_pool = _get_pool()
    conn = conn_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn_pool.putconn(conn)


def close_pool() -> None:
    """Call on app shutdown to close all pooled connections cleanly."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
