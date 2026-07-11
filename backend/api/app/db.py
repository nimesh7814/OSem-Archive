import psycopg2.pool
from dotenv import load_dotenv
from pathlib import Path
import os
import sys
import time
import logging
from typing import Optional, Type
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

class DatabaseBusyError(Exception):
    pass

class DatabaseQueryError(Exception):
    pass

class DatabaseConnectionLostError(Exception):
    pass

class DatabaseInputError(Exception):
    pass

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
except psycopg2.OperationalError as e:
    logger.error(f"Could not connect to database: {e}")
    print(f"Fatal: Could not connect to database at {os.getenv('POSTGRES_HOST')}:{os.getenv('POSTGRES_PORT')}. "
          "Check that the database is running and reachable.")
    sys.exit(1)

MAX_WAIT_TIME = int(os.getenv("MAX_WAIT_TIME_SECONDS", 10))

def run_query(query, params=None, schema: Optional[Type[BaseModel]] = None, max_wait=MAX_WAIT_TIME, interval=2):
    connection = None
    elapsed = 0
    while connection is None:
        try:
            connection = pool.getconn()
        except psycopg2.pool.PoolError:
            if elapsed >= max_wait:
                raise DatabaseBusyError("Server busy, please try again")
            time.sleep(interval)
            elapsed += interval

    try:
        cursor = connection.cursor()
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()

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
        pool.putconn(connection)
