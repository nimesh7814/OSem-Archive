CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_cron;         


CREATE TABLE IF NOT EXISTS stations (
    st_uuid     INT                   GENERATED ALWAYS AS IDENTITY,
    st_id       VARCHAR(24)           NOT NULL,
    boxtype     TEXT,
    exposure    TEXT,
    model       TEXT,
    geometry    GEOMETRY(Point, 4326) NOT NULL,
    region      TEXT,
    country     TEXT,
    init_date   TIMESTAMPTZ,

    CONSTRAINT pk_stations       PRIMARY KEY (st_uuid),
    CONSTRAINT uq_stations_st_id UNIQUE      (st_id)
);

-- Spatial index for geo queries (e.g. find stations near a point)
CREATE INDEX IF NOT EXISTS idx_stations_geometry
    ON stations USING GIST (geometry);


CREATE TABLE IF NOT EXISTS sensors (
    se_uuid     INT          GENERATED ALWAYS AS IDENTITY,
    se_id       VARCHAR(24)  NOT NULL,
    st_uuid     INT          NOT NULL,
    title       TEXT,
    unit        TEXT,
    info        TEXT,
    type        TEXT,
    init_date   TIMESTAMPTZ,

    CONSTRAINT pk_sensors        PRIMARY KEY (se_uuid),
    CONSTRAINT uq_sensors_se_id  UNIQUE      (se_id),
    CONSTRAINT fk_sensors_st_uuid
        FOREIGN KEY (st_uuid)
        REFERENCES stations (st_uuid)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS readings (
    se_uuid     INT              NOT NULL,
    time        TIMESTAMPTZ      NOT NULL,
    value       DOUBLE PRECISION NOT NULL
);

-- Convert to hypertable, partitioned by time only
SELECT create_hypertable(
    'readings',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists       => TRUE
);

-- Enforce uniqueness: one value per sensor per timestamp
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings
    ON readings (se_uuid, time);

ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_orderby   = 'time DESC',
    timescaledb.compress_segmentby = 'se_uuid'
);


-- Hourly summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_hourly
WITH (timescaledb.continuous) AS
SELECT
    se_uuid,
    time_bucket('1 hour', time)  AS bucket,
    AVG(value)                   AS avg_value
FROM readings
GROUP BY se_uuid, time_bucket('1 hour', time)
WITH NO DATA;

-- Daily summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_daily
WITH (timescaledb.continuous) AS
SELECT
    se_uuid,
    time_bucket('1 day', time)   AS bucket,
    AVG(value)                   AS avg_value
FROM readings
GROUP BY se_uuid, time_bucket('1 day', time)
WITH NO DATA;

-- Monthly summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_monthly
WITH (timescaledb.continuous) AS
SELECT
    se_uuid,
    time_bucket('1 month', time) AS bucket,
    AVG(value)                   AS avg_value
FROM readings
GROUP BY se_uuid, time_bucket('1 month', time)
WITH NO DATA;

-- Yearly summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_yearly
WITH (timescaledb.continuous) AS
SELECT
    se_uuid,
    time_bucket('1 year', time)  AS bucket,
    AVG(value)                   AS avg_value
FROM readings
GROUP BY se_uuid, time_bucket('1 year', time)
WITH NO DATA;


CREATE TABLE IF NOT EXISTS downloads (
    dl_uuid     INT     GENERATED ALWAYS AS IDENTITY,
    type        TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT pk_downloads PRIMARY KEY (dl_uuid)
);



-- summary_table
CREATE TABLE IF NOT EXISTS summary_table (
    id          INT     GENERATED ALWAYS AS IDENTITY,
    country     TEXT,
    region      TEXT,
    stations    BIGINT  NOT NULL DEFAULT 0,
    sensors     BIGINT  NOT NULL DEFAULT 0,
    readings    BIGINT  NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_summary_table PRIMARY KEY (id),
    -- One row per (country, region) pair; NULLs treated as distinct buckets.
    CONSTRAINT uq_summary_country_region UNIQUE NULLS NOT DISTINCT (country, region)
);

-- Index to speed up country/region look-ups
CREATE INDEX IF NOT EXISTS idx_summary_country_region
    ON summary_table (country, region);