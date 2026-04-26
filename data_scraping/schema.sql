-- OpenSenseMap Schema
-- Target: TimescaleDB (PostgreSQL 16 + TimescaleDB + PostGIS)
-- Applied automatically on first container start via docker-entrypoint-initdb.d

-- Extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- OpenSenseMap public IDs are 24-char lowercase hex strings.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'osm_public_id') THEN
        CREATE DOMAIN osm_public_id AS VARCHAR(24)
            CHECK (VALUE ~ '^[0-9a-f]{24}$');
    END IF;
END $$;

-- 1. Stations
CREATE TABLE IF NOT EXISTS stations (
    station_id  osm_public_id PRIMARY KEY,
    name        TEXT,
    box_type    TEXT,
    exposure    TEXT,
    geometry    GEOMETRY(Point, 4326),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stations_geometry
    ON stations USING GIST (geometry);

-- 2. Sensors
CREATE TABLE IF NOT EXISTS sensors (
    sensor_id   osm_public_id PRIMARY KEY,
    station_id  osm_public_id NOT NULL REFERENCES stations(station_id) ON DELETE CASCADE,
    title       TEXT,
    sensor_type TEXT,
    unit        TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sensors_station
    ON sensors (station_id);

-- 3. Readings  (TimescaleDB hypertable, partitioned by recorded_at)
CREATE TABLE IF NOT EXISTS readings (
    -- Ingested timestamps are expected to be RFC 3339 UTC ("...Z").
    -- TIMESTAMPTZ stores them as absolute UTC instants.
    recorded_at TIMESTAMPTZ  NOT NULL,
    sensor_id   osm_public_id NOT NULL REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    value       DOUBLE PRECISION
);

-- Backward-compatible hardening for databases created before osm_public_id.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_stations_station_id_hex24'
    ) THEN
        ALTER TABLE stations
            ADD CONSTRAINT chk_stations_station_id_hex24
            CHECK (station_id ~ '^[0-9a-f]{24}$') NOT VALID;
        ALTER TABLE stations VALIDATE CONSTRAINT chk_stations_station_id_hex24;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sensors_sensor_id_hex24'
    ) THEN
        ALTER TABLE sensors
            ADD CONSTRAINT chk_sensors_sensor_id_hex24
            CHECK (sensor_id ~ '^[0-9a-f]{24}$') NOT VALID;
        ALTER TABLE sensors VALIDATE CONSTRAINT chk_sensors_sensor_id_hex24;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sensors_station_id_hex24'
    ) THEN
        ALTER TABLE sensors
            ADD CONSTRAINT chk_sensors_station_id_hex24
            CHECK (station_id ~ '^[0-9a-f]{24}$') NOT VALID;
        ALTER TABLE sensors VALIDATE CONSTRAINT chk_sensors_station_id_hex24;
    END IF;
END $$;

SELECT create_hypertable(
    'readings',
    'recorded_at',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists       => TRUE
);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
    ON readings (sensor_id, recorded_at DESC);

-- 4. Compression  (chunks older than 1 day are compressed ~10-20x)
ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby   = 'recorded_at DESC'
);

SELECT add_compression_policy(
    'readings',
    compress_after => INTERVAL '1 day',
    if_not_exists  => TRUE
);

-- 5. Continuous Aggregates

-- Hourly rollup
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', recorded_at) AS bucket,
    sensor_id,
    AVG(value)   AS avg_value,
    MIN(value)   AS min_value,
    MAX(value)   AS max_value,
    COUNT(*)     AS reading_count
FROM readings
GROUP BY bucket, sensor_id
WITH NO DATA;

-- Daily rollup  (built on hourly)
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', bucket)                            AS bucket,
    sensor_id,
    SUM(avg_value * reading_count) / SUM(reading_count)    AS avg_value,
    MIN(min_value)                                          AS min_value,
    MAX(max_value)                                          AS max_value,
    SUM(reading_count)                                      AS reading_count
FROM sensor_data_hourly
GROUP BY time_bucket('1 day', bucket), sensor_id
WITH NO DATA;

-- Monthly rollup  (built on daily)
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_monthly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 month', bucket)                          AS bucket,
    sensor_id,
    SUM(avg_value * reading_count) / SUM(reading_count)    AS avg_value,
    MIN(min_value)                                          AS min_value,
    MAX(max_value)                                          AS max_value,
    SUM(reading_count)                                      AS reading_count
FROM sensor_data_daily
GROUP BY time_bucket('1 month', bucket), sensor_id
WITH NO DATA;

-- Yearly rollup  (built on monthly)
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_yearly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 year', bucket)                           AS bucket,
    sensor_id,
    SUM(avg_value * reading_count) / SUM(reading_count)    AS avg_value,
    MIN(min_value)                                          AS min_value,
    MAX(max_value)                                          AS max_value,
    SUM(reading_count)                                      AS reading_count
FROM sensor_data_monthly
GROUP BY time_bucket('1 year', bucket), sensor_id
WITH NO DATA;

-- 6. Continuous Aggregate Refresh Policies
SELECT add_continuous_aggregate_policy(
    'sensor_data_hourly',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy(
    'sensor_data_daily',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy(
    'sensor_data_monthly',
    start_offset      => INTERVAL '3 months',
    end_offset        => INTERVAL '1 month',
    schedule_interval => INTERVAL '1 month',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy(
    'sensor_data_yearly',
    start_offset      => INTERVAL '3 years',
    end_offset        => INTERVAL '1 year',
    schedule_interval => INTERVAL '1 year',
    if_not_exists     => TRUE
);

-- 7. Scraper bookkeeping  (internal — not part of the domain schema)
--    Tracks which archive dates have been fully committed so the scraper can
--    safely resume after a stop or crash without double-inserting data.

CREATE TABLE IF NOT EXISTS _scraper_processed_dates (
    date_str     DATE        PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
