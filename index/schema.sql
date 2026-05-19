CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "postgis";

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        CREATE EXTENSION IF NOT EXISTS "pg_cron";
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS index_log (
    inx_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    date DATE NOT NULL UNIQUE,
    indexed_at TIMESTAMPTZ DEFAULT now(),
    station_count INT DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    CONSTRAINT valid_status CHECK (
        status IN ('pending', 'success', 'partial', 'failed')
    )
);


CREATE TABLE IF NOT EXISTS stations (
    st_id TEXT PRIMARY KEY,
    name TEXT,
    exposure TEXT,
    model TEXT,
    location GEOMETRY(POINT, 4326) DEFAULT NULL,
    country TEXT,
    region TEXT,
    fs_date DATE,
    ls_date DATE,
    recorded_at TIMESTAMPTZ DEFAULT now()
);


CREATE TABLE IF NOT EXISTS station_dates (
    st_id TEXT NOT NULL REFERENCES stations(st_id) ON DELETE CASCADE,
    date DATE NOT NULL,
    folder_url TEXT,
    size_mb NUMERIC(20, 6),
    PRIMARY KEY (st_id, date)
);


CREATE TABLE IF NOT EXISTS sensors (
    se_id TEXT PRIMARY KEY,
    st_id TEXT NOT NULL REFERENCES stations(st_id) ON DELETE CASCADE,
    title TEXT,
    type TEXT,
    category TEXT,
    unit TEXT,
    recorded_at TIMESTAMPTZ DEFAULT now()
);


CREATE TABLE IF NOT EXISTS sensor_files (
    se_id TEXT NOT NULL REFERENCES sensors(se_id) ON DELETE CASCADE,
    st_id TEXT NOT NULL REFERENCES stations(st_id) ON DELETE CASCADE,
    date DATE NOT NULL,
    csv_url TEXT NOT NULL,
    size_mb NUMERIC(20, 6),
    PRIMARY KEY (se_id, date)
);


CREATE TABLE IF NOT EXISTS sensor_dates (
    se_id TEXT NOT NULL REFERENCES sensors(se_id) ON DELETE CASCADE,
    date DATE NOT NULL,
    data_available SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY (se_id, date)
);


CREATE TABLE IF NOT EXISTS readings (
    re_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    se_id TEXT NOT NULL REFERENCES sensors(se_id) ON DELETE CASCADE,
    st_id TEXT NOT NULL REFERENCES stations(st_id) ON DELETE CASCADE,
    date DATE NOT NULL,
    recorded_at TIMESTAMPTZ DEFAULT now(),
    min_value NUMERIC,
    max_value NUMERIC,
    avg_value NUMERIC,
    count INT DEFAULT 0,
    UNIQUE(se_id, date)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_readings
    ON readings (se_id, date);

SELECT create_hypertable(
    'readings',               
    'date',               
    chunk_time_interval => interval '30 days',  
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS countries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    country TEXT,
    region TEXT,
    stations INT,
    sensors INT,
    geometry GEOMETRY(POLYGON, 4326)
);

CREATE TABLE IF NOT EXISTS summary (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stations INT,
    sensor_types INT,
    readings INT,
    countries INT,
    regions INT,
    last_updated TIMESTAMPTZ DEFAULT now()
);



-- Indexes

CREATE INDEX IF NOT EXISTS idx_station_dates_date
    ON station_dates(date);

CREATE INDEX IF NOT EXISTS idx_sensor_files_date
    ON sensor_files(date);

CREATE INDEX IF NOT EXISTS idx_readings_date
    ON readings(date);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_date
    ON readings(se_id, date);

CREATE INDEX IF NOT EXISTS idx_readings_station_date
    ON readings(st_id, date);

CREATE INDEX IF NOT EXISTS idx_stations_country
    ON stations(country);

CREATE INDEX IF NOT EXISTS idx_stations_region
    ON stations(country, region);

CREATE INDEX IF NOT EXISTS idx_stations_location
    ON stations USING GIST (location);

CREATE INDEX IF NOT EXISTS idx_country_location
    ON countries USING GIST (geometry);

CREATE INDEX IF NOT EXISTS idx_sensors_station
    ON sensors(st_id);

CREATE INDEX IF NOT EXISTS idx_sensors_category
    ON sensors(category);