CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS stations (
    st_id VARCHAR(24) NOT NULL,
    boxtype TEXT,
    exposure TEXT,
    model TEXT,
    geometry GEOMETRY(Point, 4326) NOT NULL,
    region TEXT,
    country TEXT,
    init_date TIMESTAMPTZ,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_stations PRIMARY KEY (st_id)
);

CREATE INDEX IF NOT EXISTS idx_stations_geometry
    ON stations USING GIST (geometry);


CREATE TABLE IF NOT EXISTS sensors (
    se_id VARCHAR(24) NOT NULL,
    st_id VARCHAR(24) NOT NULL,
    title TEXT,
    unit TEXT,
    info TEXT,
    type TEXT,
    init_date TIMESTAMPTZ,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_sensors PRIMARY KEY (se_id),
    CONSTRAINT fk_sensors_st_id
        FOREIGN KEY (st_id)            
        REFERENCES stations (st_id)    
        ON UPDATE CASCADE
        ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS readings (
    se_id VARCHAR(24) NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION NOT NULL,

    CONSTRAINT fk_readings_se_id
        FOREIGN KEY (se_id) 
        REFERENCES sensors (se_id) 
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

SELECT create_hypertable(
    'readings',
    'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_readings
    ON readings (se_id, time); 

ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'time DESC',
    timescaledb.compress_segmentby = 'se_id'
);



-- Daily summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_daily
WITH (timescaledb.continuous) AS
SELECT
    se_id,                                              
    time_bucket('1 day',  time) AS bucket,
    AVG(value) AS avg_value
FROM readings
GROUP BY se_id, time_bucket('1 day', time) 
WITH NO DATA;

-- Monthly summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_monthly
WITH (timescaledb.continuous) AS
SELECT
    se_id, 
    time_bucket('1 month', time) AS bucket,
    AVG(value) AS avg_value
FROM readings
GROUP BY se_id, time_bucket('1 month', time) 
WITH NO DATA;

-- Yearly summary
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_yearly
WITH (timescaledb.continuous) AS
SELECT
    se_id,                               
    time_bucket('1 year', time) AS bucket,
    AVG(value) AS avg_value
FROM readings
GROUP BY se_id, time_bucket('1 year', time)
WITH NO DATA;


SELECT add_continuous_aggregate_policy('readings_daily',
    start_offset => INTERVAL '7 days',
    end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

SELECT add_continuous_aggregate_policy('readings_monthly',
    start_offset => INTERVAL '3 months',
    end_offset => INTERVAL '1 month',
    schedule_interval => INTERVAL '1 month',
    if_not_exists => TRUE
);

SELECT add_continuous_aggregate_policy('readings_yearly',
    start_offset => INTERVAL '3 years',
    end_offset => INTERVAL '1 year',
    schedule_interval => INTERVAL '1 year',
    if_not_exists => TRUE
);