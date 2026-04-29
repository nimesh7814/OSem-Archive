CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        BEGIN
            EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_cron';
        EXCEPTION
            WHEN undefined_object OR undefined_file OR invalid_parameter_value OR invalid_catalog_name OR insufficient_privilege THEN
                RAISE NOTICE 'pg_cron is available but could not be initialized; skipping cron jobs (%).', SQLERRM;
        END;
    ELSE
        RAISE NOTICE 'pg_cron extension is not available; skipping cron jobs.';
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'osem_code') THEN
        CREATE DOMAIN osem_code AS VARCHAR(24)
            CHECK (VALUE ~ '^[0-9a-f]{24}$');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS stations (
    station_dbid  BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id    osem_code   NOT NULL,
    name          TEXT,
    box_type      TEXT,
    exposure      TEXT,
    geometry      GEOMETRY(Point, 4326),
    station_date  DATE
);

-- Unique index so lookups by the original hex ID remain fast
CREATE UNIQUE INDEX IF NOT EXISTS uq_stations_station_id ON stations (station_id);

CREATE INDEX IF NOT EXISTS idx_stations_geometry
    ON stations USING GIST (geometry);

CREATE INDEX IF NOT EXISTS idx_stations_station_date
    ON stations (station_date);


CREATE TABLE IF NOT EXISTS sensors (
    sensor_dbid  BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sensor_id    osem_code   NOT NULL,
    station_dbid BIGINT      NOT NULL REFERENCES stations(station_dbid) ON DELETE CASCADE,
    station_id   osem_code   NOT NULL,
    title        TEXT,
    sensor_info  TEXT,
    type         TEXT,
    unit         TEXT,
    sensor_date  DATE 
);

-- Unique index so lookups by the original hex ID remain fast
CREATE UNIQUE INDEX IF NOT EXISTS uq_sensors_sensor_id ON sensors (sensor_id);

CREATE INDEX IF NOT EXISTS idx_sensors_station ON sensors (station_dbid);

CREATE INDEX IF NOT EXISTS idx_sensors_sensor_date ON sensors (sensor_date);

CREATE TABLE IF NOT EXISTS readings (
    recorded_at  TIMESTAMPTZ NOT NULL,
    sensor_dbid  BIGINT      NOT NULL REFERENCES sensors(sensor_dbid) ON DELETE CASCADE,
    value        DOUBLE PRECISION
);

-- Backward-compatible hardening: ensure hex-24 format on the natural key columns.
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
    chunk_time_interval => INTERVAL '1 week',   -- auto-adjusted at runtime
    if_not_exists       => TRUE
);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
    ON readings (sensor_dbid, recorded_at DESC);

-- Hourly rollup
-- Joins back to sensors to expose sensor_id (hex) for API queries,
-- while grouping internally on the integer sensor_dbid for efficiency.
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', r.recorded_at) AS bucket,
    r.sensor_dbid,
    AVG(r.value)   AS avg_value,
    MIN(r.value)   AS min_value,
    MAX(r.value)   AS max_value,
    COUNT(*)       AS reading_count
FROM readings r
GROUP BY bucket, r.sensor_dbid
WITH NO DATA;

-- Daily rollup  (built on hourly)
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', bucket)                            AS bucket,
    sensor_dbid,
    SUM(avg_value * reading_count) / SUM(reading_count)    AS avg_value,
    MIN(min_value)                                          AS min_value,
    MAX(max_value)                                          AS max_value,
    SUM(reading_count)                                      AS reading_count
FROM sensor_data_hourly
GROUP BY time_bucket('1 day', bucket), sensor_dbid
WITH NO DATA;

-- Monthly rollup  (built on daily)
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_monthly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 month', bucket)                          AS bucket,
    sensor_dbid,
    SUM(avg_value * reading_count) / SUM(reading_count)    AS avg_value,
    MIN(min_value)                                          AS min_value,
    MAX(max_value)                                          AS max_value,
    SUM(reading_count)                                      AS reading_count
FROM sensor_data_daily
GROUP BY time_bucket('1 month', bucket), sensor_dbid
WITH NO DATA;

-- Yearly rollup  (built on monthly)
CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_data_yearly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 year', bucket)                           AS bucket,
    sensor_dbid,
    SUM(avg_value * reading_count) / SUM(reading_count)    AS avg_value,
    MIN(min_value)                                          AS min_value,
    MAX(max_value)                                          AS max_value,
    SUM(reading_count)                                      AS reading_count
FROM sensor_data_monthly
GROUP BY time_bucket('1 year', bucket), sensor_dbid
WITH NO DATA;

CREATE TABLE IF NOT EXISTS _scraper_processed_dates (
    date_str     DATE        PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS boundaries (
    region_code  TEXT    PRIMARY KEY,
    country_code CHAR(3),
    country_name TEXT,
    region_name  TEXT,
    geometry     GEOMETRY(MultiPolygon, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_boundaries_geometry
    ON boundaries USING GIST (geometry);

CREATE INDEX IF NOT EXISTS idx_boundaries_country_code
    ON boundaries (country_code);

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

CREATE OR REPLACE FUNCTION stations_set_region_code()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.geometry IS NULL THEN
        NEW.region_code := NULL;
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.geometry IS NOT DISTINCT FROM NEW.geometry THEN
        RETURN NEW;
    END IF;

    SELECT region_code
      INTO NEW.region_code
      FROM boundaries
     WHERE ST_Within(NEW.geometry, geometry)
     LIMIT 1;

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

CREATE OR REPLACE FUNCTION backfill_station_region_code()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE stations s
    SET region_code = (
        SELECT b.region_code
          FROM boundaries b
         WHERE ST_Within(s.geometry, b.geometry)
         LIMIT 1
    )
    WHERE s.geometry IS NOT NULL
      AND s.region_code IS NULL;

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

CREATE TABLE IF NOT EXISTS _readings_meta (
    id               INT PRIMARY KEY DEFAULT 1,   -- single-row table
    last_min_at      TIMESTAMPTZ,                 -- lowest  recorded_at seen
    last_max_at      TIMESTAMPTZ,                 -- highest recorded_at seen
    last_chunk_ivl   INTERVAL,
    last_adjusted_at TIMESTAMPTZ,
    CONSTRAINT single_row CHECK (id = 1)
);

INSERT INTO _readings_meta (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION auto_adjust_readings()
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_min         TIMESTAMPTZ;
    v_max         TIMESTAMPTZ;
    v_span_days   DOUBLE PRECISION;
    v_interval    INTERVAL;
    v_meta        _readings_meta%ROWTYPE;
    v_refresh_min TIMESTAMPTZ;
    v_refresh_max TIMESTAMPTZ;
BEGIN
    -- Step 1: Read the actual recorded_at range from the hypertable.
    --   MIN/MAX scan is fast because TimescaleDB maintains per-chunk
    --   statistics and can answer this without a full table scan.
    SELECT MIN(recorded_at), MAX(recorded_at)
      INTO v_min, v_max
      FROM readings;

    IF v_min IS NULL THEN
        RAISE NOTICE 'auto_adjust_readings: readings is empty, skipping.';
        RETURN;
    END IF;

    -- Step 2: Compare against last known range.
    SELECT * INTO v_meta FROM _readings_meta WHERE id = 1;

    IF v_meta.last_min_at IS NOT DISTINCT FROM v_min
       AND v_meta.last_max_at IS NOT DISTINCT FROM v_max THEN
        RAISE NOTICE 'auto_adjust_readings: recorded_at range unchanged (% → %), skipping.',
            v_min, v_max;
        RETURN;
    END IF;

    -- Step 3: Pick chunk interval based on total span.
    v_span_days := EXTRACT(EPOCH FROM (v_max - v_min)) / 86400.0;

    IF v_span_days > 5 * 365 THEN
        v_interval := INTERVAL '3 months';
    ELSIF v_span_days > 365 THEN
        v_interval := INTERVAL '1 month';
    ELSE
        v_interval := INTERVAL '1 week';
    END IF;

    RAISE NOTICE 'auto_adjust_readings: span=% days → chunk_interval=%',
        round(v_span_days::numeric, 1), v_interval;

    -- Step 4: Update chunk interval only if it changed.
    IF v_meta.last_chunk_ivl IS DISTINCT FROM v_interval THEN
        PERFORM set_chunk_time_interval('readings', v_interval);
        RAISE NOTICE 'auto_adjust_readings: chunk interval updated to %', v_interval;
    END IF;

    IF v_meta.last_min_at IS NULL THEN
        -- First run: full range
        v_refresh_min := date_trunc('hour', v_min) - INTERVAL '1 hour';
        v_refresh_max := date_trunc('hour', v_max) + INTERVAL '1 hour';
    ELSE
        -- Delta refresh: only the slice that is new since last run.
        -- Use LEAST on the low end in case data was backfilled further
        -- into the past than before.
        v_refresh_min := date_trunc('hour', LEAST(v_min, v_meta.last_min_at))
                         - INTERVAL '1 hour';
        v_refresh_max := date_trunc('hour', v_max) + INTERVAL '1 hour';
    END IF;

    RAISE NOTICE 'auto_adjust_readings: refreshing aggregates % → %',
        v_refresh_min, v_refresh_max;

    -- Refresh in dependency order: hourly first, then each coarser level
    -- reads from the level below it — so order matters.
    CALL refresh_continuous_aggregate('sensor_data_hourly',  v_refresh_min, v_refresh_max);
    RAISE NOTICE 'auto_adjust_readings: sensor_data_hourly done.';

    CALL refresh_continuous_aggregate('sensor_data_daily',   v_refresh_min, v_refresh_max);
    RAISE NOTICE 'auto_adjust_readings: sensor_data_daily done.';

    CALL refresh_continuous_aggregate('sensor_data_monthly', v_refresh_min, v_refresh_max);
    RAISE NOTICE 'auto_adjust_readings: sensor_data_monthly done.';

    CALL refresh_continuous_aggregate('sensor_data_yearly',  v_refresh_min, v_refresh_max);
    RAISE NOTICE 'auto_adjust_readings: sensor_data_yearly done.';

    -- Step 6: Persist the new high-water marks.
    UPDATE _readings_meta SET
        last_min_at      = v_min,
        last_max_at      = v_max,
        last_chunk_ivl   = v_interval,
        last_adjusted_at = NOW()
    WHERE id = 1;

    RAISE NOTICE 'auto_adjust_readings: complete. Range now % → %', v_min, v_max;
END;
$$;

SELECT auto_adjust_readings();

DO $$
BEGIN
    IF to_regnamespace('cron') IS NULL THEN
        RAISE NOTICE 'pg_cron schema is not available; skipping auto_adjust_readings schedule.';
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM cron.job
         WHERE jobname = 'auto_adjust_readings'
    ) THEN
        PERFORM cron.schedule(
            'auto_adjust_readings',
            '0 0 * * *',
            'SELECT auto_adjust_readings()'
        );
    END IF;
END;
$$;
