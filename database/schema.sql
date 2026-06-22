CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_cron;


-- Tables
CREATE TABLE IF NOT EXISTS stations (
    st_id VARCHAR(24) NOT NULL,
    boxtype TEXT,
    exposure TEXT,
    model TEXT,
    geometry GEOMETRY(Point, 4326),
    region TEXT,
    country TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_stations PRIMARY KEY (st_id),
    CONSTRAINT uq_stations_id UNIQUE (st_id)
);

CREATE TABLE IF NOT EXISTS sensors (
    se_id VARCHAR(24) NOT NULL,
    st_id VARCHAR(24) NOT NULL,
    title TEXT,
    unit TEXT,
    sensor_type TEXT,
    category TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_sensors PRIMARY KEY (se_id),
    CONSTRAINT uq_sensors UNIQUE (se_id),
    CONSTRAINT fk_sensors_st_id FOREIGN KEY (st_id) REFERENCES stations (st_id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS readings (
    se_id VARCHAR(24) NOT NULL,
    st_id VARCHAR(24) NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_readings_se_id FOREIGN KEY (se_id) REFERENCES sensors (se_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT fk_readings_st_id FOREIGN KEY (st_id) REFERENCES stations (st_id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summary (
    su_id INT GENERATED ALWAYS AS IDENTITY,
    country TEXT,
    region TEXT,
    stations BIGINT NOT NULL DEFAULT 0,
    sensors BIGINT NOT NULL DEFAULT 0,
    readings BIGINT NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_summary PRIMARY KEY (su_id),
    CONSTRAINT uq_summary_country_region UNIQUE NULLS NOT DISTINCT (country, region)
);


-- Indexes
CREATE INDEX IF NOT EXISTS idx_stations_geometry ON stations USING GIST (geometry);
CREATE INDEX IF NOT EXISTS idx_summary_country_region ON summary (country, region);
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings ON readings (se_id, time);

-- Hypertable
SELECT create_hypertable(
    'readings',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    migrate_data => TRUE,
    if_not_exists => TRUE
);

-- Compression
ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_orderby  = 'time DESC',
    timescaledb.compress_segmentby = 'se_id'
);

SELECT add_compression_policy(
    'readings',
    compress_after => INTERVAL '1 day',
    if_not_exists => TRUE
);


-- Continuous aggregates 
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_daily
WITH (timescaledb.continuous) AS
SELECT
    se_id,
    time_bucket('1 day', time) AS bucket,
    AVG(value) AS avg_value
FROM readings
GROUP BY se_id, time_bucket('1 day', time)
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_weekly
WITH (timescaledb.continuous) AS
SELECT
    se_id,
    time_bucket('1 week', bucket) AS bucket,
    AVG(avg_value) AS avg_value
FROM readings_daily
GROUP BY se_id, time_bucket('1 week', bucket)
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_monthly
WITH (timescaledb.continuous) AS
SELECT
    se_id,
    time_bucket('30 days', bucket) AS bucket,
    AVG(avg_value) AS avg_value
FROM readings_daily
GROUP BY se_id, time_bucket('30 days', bucket)
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_yearly
WITH (timescaledb.continuous) AS
SELECT
    se_id,
    time_bucket('364 days', bucket) AS bucket,
    AVG(avg_value) AS avg_value
FROM readings_weekly
GROUP BY se_id, time_bucket('364 days', bucket)
WITH NO DATA;

-- Refresh policies
SELECT add_continuous_aggregate_policy('readings_daily',
    start_offset => INTERVAL '7 days',
    end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

SELECT add_continuous_aggregate_policy('readings_weekly',
    start_offset => INTERVAL '3 weeks',
    end_offset => INTERVAL '1 week',
    schedule_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

SELECT add_continuous_aggregate_policy('readings_monthly',
    start_offset => INTERVAL '90 days',
    end_offset => INTERVAL '30 days',
    schedule_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

SELECT add_continuous_aggregate_policy('readings_yearly',
    start_offset => INTERVAL '1092 days',
    end_offset => INTERVAL '364 days',
    schedule_interval => INTERVAL '364 days',
    if_not_exists => TRUE
);