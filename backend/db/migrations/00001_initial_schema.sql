-- 00001_initial_schema.sql
-- openSenseMap Archive — initial schema

BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_cron;


-- regions
CREATE TABLE regions (
    id SERIAL PRIMARY KEY,
    country TEXT NOT NULL,
    region TEXT NOT NULL,
    geometry GEOMETRY(MULTIPOLYGON, 4326),
    CONSTRAINT uq_country_region UNIQUE (country, region)
);

CREATE INDEX idx_regions_geometry ON regions USING GIST (geometry);


-- boxes
CREATE TABLE boxes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    box_type TEXT,
    exposure TEXT,
    model TEXT,
    location GEOMETRY(POINT, 4326),
    region_id INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_measurement_at TIMESTAMPTZ
);

CREATE INDEX idx_boxes_region ON boxes (region_id);
CREATE INDEX idx_boxes_location ON boxes USING GIST (location);
CREATE INDEX idx_boxes_box_type ON boxes (box_type);
CREATE INDEX idx_boxes_exposure ON boxes (exposure);


-- sensors
CREATE TABLE sensors (
    id TEXT PRIMARY KEY,
    box_id TEXT NOT NULL,
    title TEXT NOT NULL,
    unit TEXT,
    sensor_type TEXT
);

CREATE INDEX idx_sensors_box_id ON sensors (box_id);


-- measurements
CREATE TABLE measurements (
    time TIMESTAMPTZ NOT NULL,
    sensor_id TEXT NOT NULL,
    value DOUBLE PRECISION
);


-- Summary Table
CREATE TABLE summary (
    id integer PRIMARY KEY DEFAULT 1,
    summary_date date NOT NULL,
    stations integer,
    sensors integer,
    readings integer,
    countries integer,
    updated_at timestamptz
);

-- Hypertable
SELECT create_hypertable(
    'measurements',
    'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- Compression
ALTER TABLE measurements SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy(
    'measurements',
    INTERVAL '7 days'
);

-- Filter by sensor_id directly in queries
CREATE INDEX idx_measurements_sensor_time
ON measurements (sensor_id, time DESC);


-- ingest log
CREATE TABLE ingest_log (
    id SERIAL PRIMARY KEY,
    box_id TEXT NOT NULL,
    archive_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done', 'failed')),
    row_count INT DEFAULT 0,
    loaded_at TIMESTAMPTZ,
    CONSTRAINT uq_ingest_log_box_date UNIQUE (box_id, archive_date)
);

CREATE INDEX idx_ingest_log_status ON ingest_log (status);


-- export jobs
CREATE TABLE export_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    box_id TEXT,
    phenomenon TEXT,
    date_from TIMESTAMPTZ,
    date_to TIMESTAMPTZ,
    format TEXT NOT NULL DEFAULT 'csv'
        CHECK (format IN ('csv', 'json')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    row_count INT,
    file_url TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_export_job_status ON export_job (status);

-- Refresh summary function
CREATE OR REPLACE FUNCTION public.refresh_summary()
 RETURNS void
 LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO summary (
        id,
        summary_date,
        stations,
        sensors,
        readings,
        countries,
        updated_at
    )
    VALUES (
        1,
        CURRENT_DATE,
        (SELECT COUNT(*) FROM boxes),
        (SELECT COUNT(*) FROM sensors),
        (SELECT COUNT(*) FROM measurements),
        (SELECT COUNT(DISTINCT r.country)
         FROM boxes b
         JOIN regions r ON b.region_id = r.id),
        now()
    )
    ON CONFLICT (id)
    DO UPDATE SET
        summary_date = EXCLUDED.summary_date,
        stations = EXCLUDED.stations,
        sensors = EXCLUDED.sensors,
        readings = EXCLUDED.readings,
        countries = EXCLUDED.countries,
        updated_at = now();
END;
$function$;

SELECT refresh_summary();

-- Schedule nightly summary at Midnight
SELECT cron.schedule(
    'refresh_summary_midnight',
    '0 0 * * *',
    $$ SELECT refresh_summary(); $$
);

COMMIT;