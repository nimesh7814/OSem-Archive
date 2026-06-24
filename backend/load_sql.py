#!/usr/bin/env python3
"""
load_sql.py
-----------
Executes any SQL file against the TimescaleDB / PostgreSQL database defined in .env

Requirements:
    pip install psycopg2-binary python-dotenv

Usage:
    python load_sql.py schema.sql                   # load schema
    python load_sql.py setup_cron_jobs.sql          # activate cron jobs
    python load_sql.py path/to/any.sql              # any SQL file
    python load_sql.py schema.sql --env .env.prod   # custom env file
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute a SQL file against the TimescaleDB / PostgreSQL database."
    )
    parser.add_argument(
        "sql",
        help="Path to the SQL file to execute (e.g. schema.sql, setup_cron_jobs.sql)",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the .env file (default: .env)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_env(env_path: str) -> None:
    if not os.path.exists(env_path):
        print(f"[WARN] .env file not found at '{env_path}'. Falling back to environment variables.")
    else:
        load_dotenv(env_path)
        print(f"[INFO] Loaded environment from '{env_path}'")


def read_sql(sql_path: str) -> str:
    if not os.path.exists(sql_path):
        print(f"[ERROR] SQL file not found: '{sql_path}'")
        sys.exit(1)
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
    print(f"[INFO] Read '{sql_path}' ({len(sql)} bytes)")
    return sql


def get_connection_params() -> dict:
    params = {
        "host":     os.getenv("POSTGRES_HOST", "localhost"),
        "port":     os.getenv("POSTGRES_PORT", "5432"),
        "dbname":   os.getenv("POSTGRES_DB"),
        "user":     os.getenv("POSTGRES_USER"),
        "password": os.getenv("POSTGRES_PASSWORD"),
    }

    missing = [k for k, v in params.items() if not v]
    if missing:
        print(f"[ERROR] Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    return params


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()

    load_env(args.env)
    sql = read_sql(args.sql)
    params = get_connection_params()

    print(
        f"[INFO] Connecting to {params['user']}@{params['host']}:{params['port']}"
        f"/{params['dbname']} ..."
    )

    try:
        conn = psycopg2.connect(**params)
        conn.autocommit = True          # needed for CREATE EXTENSION and TimescaleDB statements

        with conn.cursor() as cur:
            cur.execute(sql)

        conn.close()
        print(f"[OK]   '{args.sql}' executed successfully.")

    except psycopg2.OperationalError as e:
        print(f"[ERROR] Could not connect to the database:\n  {e}")
        sys.exit(1)

    except psycopg2.Error as e:
        print(f"[ERROR] SQL execution failed:\n  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
