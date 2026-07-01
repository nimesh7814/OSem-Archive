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

# Text formatting
class formats:
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    DARKCYAN = '\033[36m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

# Private pool variable setup outside of the funciton
_pool = None

# Create a simple pool connection
def init_db_pool():

    global _pool

    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=20,
            user=USER,
            password=PASSWORD,
            host=HOST,
            port=PORT,
            database=DBNAME
        )

# Get a connection
def get_connection():

    if _pool is None:
        init_db_pool()
    return _pool.getconn()

# Release the connection
def release_connection(conn):

    if _pool:
        _pool.putconn(conn)

# Execute a query
def execute_query(query, params=None):

    conn = None

    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute(query, params)
        conn.commit()

        result = cur.fetchall() if cur.description else None
        cur.close()

        return result

    except:
        if conn:
            conn.rollback()

        print(f"{formats.RED}Error:{formats.END} Could not connect to the database.")
        exit(1)

    finally:
        if conn:
            release_connection(conn)