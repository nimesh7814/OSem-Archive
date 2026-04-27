#!/bin/bash
# Migration: create read-only web_anonymous role
# Runs once automatically via docker-entrypoint-initdb.d on first DB initialisation.
# Reads DB_RO_USER and DB_RO_PASSWORD from the container environment (set in docker-compose.yml).
set -e

# Defaults in case env vars are not set
RO_USER="${DB_RO_USER:-web_anonymous}"
RO_PASSWORD="${DB_RO_PASSWORD:-osem_readonly}"

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
-- Create the role only if it does not already exist
DO \$\$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = '${RO_USER}'
    ) THEN
        CREATE ROLE ${RO_USER}
            LOGIN
            PASSWORD '${RO_PASSWORD}'
            NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
    END IF;
END \$\$;

-- Allow connecting to the database
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${RO_USER};

-- Grant USAGE on the schema
GRANT USAGE ON SCHEMA public TO ${RO_USER};

-- Read-only SELECT on all current tables and views
GRANT SELECT ON
    stations,
    sensors,
    readings,
    boundaries,
    sensor_data_hourly,
    sensor_data_daily,
    sensor_data_monthly,
    sensor_data_yearly
TO ${RO_USER};

-- Ensure future tables in public schema are also readable
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO ${RO_USER};

-- Explicitly deny access to the scraper bookkeeping table
REVOKE ALL ON _scraper_processed_dates FROM ${RO_USER};
SQL

echo "Read-only role '${RO_USER}' configured successfully."
