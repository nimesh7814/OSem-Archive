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
    geometry    GEOMETRY(Point, 4326)
    -- region_code (FK → boundaries) is added below after the boundaries table is created
);

CREATE INDEX IF NOT EXISTS idx_stations_geometry
    ON stations USING GIST (geometry);

-- 2. Sensors
CREATE TABLE IF NOT EXISTS sensors (
    sensor_id   osm_public_id PRIMARY KEY,
    station_id  osm_public_id NOT NULL REFERENCES stations(station_id) ON DELETE CASCADE,
    title       TEXT,
    sensor_info TEXT,
    type        TEXT,
    unit        TEXT
);

CREATE INDEX IF NOT EXISTS idx_sensors_station ON sensors (station_id);

-- 3. Readings  (TimescaleDB hypertable, partitioned by recorded_at)
CREATE TABLE IF NOT EXISTS readings (
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

-- 4. Compression removed — raw chunks are kept uncompressed for full write
--    throughput during the initial archive ingest.
--    To re-enable compression after ingest is complete, run:
--
--      ALTER TABLE readings SET (
--          timescaledb.compress,
--          timescaledb.compress_segmentby = 'sensor_id',
--          timescaledb.compress_orderby   = 'recorded_at DESC'
--      );
--      SELECT add_compression_policy('readings',
--          compress_after => INTERVAL '1 day', if_not_exists => TRUE);

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

-- 7. Geographic resolution is handled solely via the boundaries table (section 9).
--    The ne_countries and ne_admin1 Natural Earth tables are not used.


-- 8. Scraper bookkeeping  (internal — not part of the domain schema)
--    Tracks which archive dates have been fully committed so the scraper can
--    safely resume after a stop or crash without double-inserting data.

CREATE TABLE IF NOT EXISTS _scraper_processed_dates (
    date_str     DATE        PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 9. Admin-1 boundaries table + station.region_code foreign key
--    Populated by load_boundaries.py from data/admin_boundary.geojson.
--    GeoJSON property → column:
--      adm1_code → region_code (PK)
--      adm0_code → country_code
--      adm0_name → country_name
--      adm1_name → region_name
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS boundaries (
    region_code  TEXT    PRIMARY KEY,            -- adm1_code  e.g. "ABW-5150"
    country_code CHAR(3),                        -- adm0_code  e.g. "ARG"
    country_name TEXT,                           -- adm0_name  e.g. "Argentina"
    region_name  TEXT,                           -- adm1_name  e.g. "Entre Ríos"
    geometry     GEOMETRY(MultiPolygon, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_boundaries_geometry
    ON boundaries USING GIST (geometry);

CREATE INDEX IF NOT EXISTS idx_boundaries_country_code
    ON boundaries (country_code);

-- Add region_code FK column to stations if not present
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'stations' AND column_name = 'region_code'
    ) THEN
        ALTER TABLE stations
            ADD COLUMN region_code TEXT
                REFERENCES boundaries (region_code)
                ON DELETE SET NULL
                ON UPDATE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_stations_region_code
    ON stations (region_code);

-- Trigger: auto-resolve region_code when a station's geometry is set.
-- Exact containment (fast with GIST index). Nearest-neighbour fallback
-- for offshore / border stations.
CREATE OR REPLACE FUNCTION stations_set_region_code()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- NULL geometry → clear region_code
    IF NEW.geometry IS NULL THEN
        NEW.region_code := NULL;
        RETURN NEW;
    END IF;

    -- Skip spatial work when geometry hasn't changed on UPDATE
    IF TG_OP = 'UPDATE' AND OLD.geometry IS NOT DISTINCT FROM NEW.geometry THEN
        RETURN NEW;
    END IF;

    -- Exact containment (fast with GIST index)
    SELECT region_code
      INTO NEW.region_code
      FROM boundaries
     WHERE ST_Within(NEW.geometry, geometry)
     LIMIT 1;

    -- Nearest-neighbour fallback for offshore / border stations
    IF NEW.region_code IS NULL THEN
        SELECT region_code
          INTO NEW.region_code
          FROM boundaries
         ORDER BY geometry <-> NEW.geometry
         LIMIT 1;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_stations_set_region_code ON stations;
CREATE TRIGGER trg_stations_set_region_code
    BEFORE INSERT OR UPDATE OF geometry ON stations
    FOR EACH ROW EXECUTE FUNCTION stations_set_region_code();

-- Backfill helper — only fills NULL rows, safe to call multiple times.
CREATE OR REPLACE FUNCTION backfill_station_region_code()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    -- Exact containment pass
    UPDATE stations s
    SET region_code = (
        SELECT b.region_code
          FROM boundaries b
         WHERE ST_Within(s.geometry, b.geometry)
         LIMIT 1
    )
    WHERE s.geometry IS NOT NULL
      AND s.region_code IS NULL;

    -- Nearest-neighbour fallback for any still-unresolved stations
    UPDATE stations s
    SET region_code = (
        SELECT b.region_code
          FROM boundaries b
         ORDER BY b.geometry <-> s.geometry
         LIMIT 1
    )
    WHERE s.geometry IS NOT NULL
      AND s.region_code IS NULL;
END;
$$;
