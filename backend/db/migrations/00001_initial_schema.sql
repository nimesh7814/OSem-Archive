-- 00001_initial_schema.sql
-- openSenseMap Archive — initial schema

BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- regions
CREATE TABLE regions (
    id SERIAL PRIMARY KEY,
    country TEXT NOT NULL,
    region TEXT NOT NULL,
    geometry GEOGRAPHY(MULTIPOLYGON, 4326),
    CONSTRAINT uq_country_region UNIQUE (country, region)
);

CREATE INDEX idx_regions_country_code ON regions (id);
CREATE INDEX idx_regions_geometry ON regions USING GIST (geometry);

-- boxes
CREATE TABLE boxes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    box_type TEXT,
    exposure TEXT,
    model TEXT,
    location GEOGRAPHY(POINT, 4326),
    region_id INT REFERENCES regions(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_measurement_at TIMESTAMPTZ
);

CREATE INDEX idx_boxes_region ON boxes (region_id);
CREATE INDEX idx_boxes_location ON boxes USING GIST (location);
CREATE INDEX idx_boxes_exposure ON boxes (exposure);
CREATE INDEX idx_boxes_box_type ON boxes (box_type);

-- sensors
CREATE TABLE sensors (
    id TEXT PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    unit TEXT,
    sensor_type TEXT
);

CREATE INDEX idx_sensors_box_id ON sensors (box_id);
CREATE INDEX idx_sensors_title ON sensors (title);

-- measurements
CREATE TABLE measurements (
    time TIMESTAMPTZ NOT NULL,
    sensor_id TEXT NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
    value REAL,
    PRIMARY KEY (sensor_id, time)
);

SELECT create_hypertable(
    'measurements',
    'time',
    chunk_time_interval => INTERVAL '1 day'
);

ALTER TABLE measurements SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby = 'time DESC'
);

-- ingest_log
CREATE TABLE ingest_log (
    id SERIAL PRIMARY KEY,
    box_id TEXT NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
    archive_date DATE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')) DEFAULT 'pending',
    row_count INT DEFAULT 0,
    loaded_at TIMESTAMPTZ,
    CONSTRAINT uq_ingest_log_box_date UNIQUE (box_id, archive_date)
);

CREATE INDEX idx_ingest_log_status ON ingest_log (status);

-- export_job
CREATE TABLE export_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    box_id TEXT REFERENCES boxes(id) ON DELETE SET NULL,
    phenomenon TEXT,
    date_from TIMESTAMPTZ,
    date_to TIMESTAMPTZ,
    format TEXT NOT NULL CHECK (format IN ('csv', 'json')) DEFAULT 'csv',
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')) DEFAULT 'pending',
    row_count INT,
    file_url TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_export_job_status ON export_job (status);

COMMIT;