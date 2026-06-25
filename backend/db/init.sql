-- infra/docker/timescaledb/init.sql
-- Runs automatically on first container startup (mounted into /docker-entrypoint-initdb.d/)

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Set the database's default timezone to Europe/Berlin.
-- This affects how TIMESTAMPTZ values are *displayed* (storage is always UTC internally).
ALTER DATABASE osem_archive SET timezone TO 'Europe/Berlin';
