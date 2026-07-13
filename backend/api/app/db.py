import logging
import os
import time
import threading
import uuid
from pathlib import Path
from typing import Optional, Type

import psycopg2.pool
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)
pool = None
_pool_lock = threading.Lock()

class DatabaseBusyError(Exception):
    pass

class DatabaseQueryError(Exception):
    pass

class DatabaseConnectionLostError(Exception):
    pass

class DatabaseInputError(Exception):
    pass


def _create_pool():
    global pool

    with _pool_lock:
        if pool is not None:
            return pool

        try:
            pool = psycopg2.pool.SimpleConnectionPool(
                1,
                10,
                user=os.getenv("POSTGRES_USER"),
                password=os.getenv("POSTGRES_PASSWORD"),
                host=os.getenv("POSTGRES_HOST"),
                port=os.getenv("POSTGRES_PORT"),
                database=os.getenv("POSTGRES_DB")
            )
            logger.info("Database pool created successfully")
        except psycopg2.OperationalError as exc:
            logger.error("Could not connect to database: %s", exc)
            raise DatabaseConnectionLostError("The database is temporarily unavailable.") from exc

    return pool


def get_pool():
    if pool is not None:
        return pool
    return _create_pool()

MAX_WAIT_TIME = int(os.getenv("MAX_WAIT_TIME_SECONDS", 10))

def run_query(query, params=None, schema: Optional[Type[BaseModel]] = None, max_wait=MAX_WAIT_TIME, interval=2):
    connection = None
    elapsed = 0
    connection_pool = get_pool()
    while connection is None:
        try:
            connection = connection_pool.getconn()
        except psycopg2.pool.PoolError:
            if elapsed >= max_wait:
                raise DatabaseBusyError("Server busy, please try again")
            time.sleep(interval)
            elapsed += interval
        except psycopg2.OperationalError as e:
            logger.error(f"Lost connection to database while acquiring connection: {e}")
            raise DatabaseConnectionLostError("Lost connection to the database. Please try again.") from e

    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            columns = [desc[0] for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        if schema is not None:
            return [schema(**row) for row in rows]
        return rows

    # Handle lost connection errors
    except psycopg2.OperationalError as e:
        logger.error(f"Lost connection to database during query: {e}")
        raise DatabaseConnectionLostError("Lost connection to the database. Please try again.") from e

    # Handle query errors
    except psycopg2.Error as e:
        logger.error(f"Database query failed: {e}")
        raise DatabaseQueryError(f"Query failed: {e}") from e

    # e.g. NUL bytes in a string, which psycopg2 rejects before ever sending the query
    except ValueError as e:
        logger.warning(f"Rejected malformed query parameter: {e}")
        raise DatabaseInputError("The request contains a value the database cannot accept.") from e

    finally:
        connection_pool.putconn(connection)


def stream_query(query, params=None, max_wait=MAX_WAIT_TIME, interval=2, itersize=2000):
    connection = None
    elapsed = 0
    connection_pool = get_pool()
    while connection is None:
        try:
            connection = connection_pool.getconn()
        except psycopg2.pool.PoolError:
            if elapsed >= max_wait:
                raise DatabaseBusyError("Server busy, please try again")
            time.sleep(interval)
            elapsed += interval
        except psycopg2.OperationalError as e:
            logger.error(f"Lost connection to database while acquiring connection: {e}")
            raise DatabaseConnectionLostError("Lost connection to the database. Please try again.") from e

    cursor = None
    try:
        cursor = connection.cursor(name=f"api_stream_{uuid.uuid4().hex}")
        cursor.itersize = itersize
        cursor.execute(query, params)
        # Named cursors only populate .description after the first fetch, not right after execute().
        columns = None
        for row in cursor:
            if columns is None:
                columns = [desc[0] for desc in cursor.description]
            yield dict(zip(columns, row))

    except psycopg2.OperationalError as e:
        logger.error(f"Lost connection to database during query: {e}")
        raise DatabaseConnectionLostError("Lost connection to the database. Please try again.") from e

    except psycopg2.Error as e:
        logger.error(f"Database query failed: {e}")
        raise DatabaseQueryError(f"Query failed: {e}") from e

    except ValueError as e:
        logger.warning(f"Rejected malformed query parameter: {e}")
        raise DatabaseInputError("The request contains a value the database cannot accept.") from e

    finally:
        if cursor is not None:
            cursor.close()
        connection_pool.putconn(connection)


def check_database_connection():
    run_query("SELECT 1")
    return True
