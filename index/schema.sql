CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "postgis";

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        CREATE EXTENSION IF NOT EXISTS "pg_cron";
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS index_log (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE NOT NULL UNIQUE,
    indexed_at TIMESTAMPTZ DEFAULT now(),
    station_count INT DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',

    CONSTRAINT valid_status CHECK (
        status IN ('pending', 'success', 'partial', 'failed')
    )
);



CREATE TABLE IF NOT EXISTS stations (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    box_id TEXT NOT NULL UNIQUE,
    name TEXT,
    location GEOMETRY(POINT, 4326) DEFAULT NULL,
    country TEXT,
    region TEXT,
    city TEXT,
    fs_date DATE,
    ls_date DATE,
    init_date TIMESTAMPTZ DEFAULT now()
);



CREATE TABLE IF NOT EXISTS station_dates (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    date DATE NOT NULL,
    folder_url TEXT,

    UNIQUE(station_uuid, date)
);



CREATE TABLE IF NOT EXISTS sensors (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id TEXT NOT NULL UNIQUE,
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    title TEXT,
    type TEXT,
    category TEXT,
    unit TEXT,
    init_date TIMESTAMPTZ DEFAULT now()
);



CREATE TABLE IF NOT EXISTS sensor_files (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_uuid UUID NOT NULL REFERENCES sensors(uuid)  ON DELETE CASCADE,
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    date DATE NOT NULL,
    csv_url TEXT NOT NULL,

    UNIQUE(sensor_uuid, date)
);



CREATE TABLE IF NOT EXISTS readings (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_uuid UUID NOT NULL REFERENCES sensors(uuid)  ON DELETE CASCADE,
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    date DATE NOT NULL,
    recorded_at TIMESTAMPTZ,
    min_value DOUBLE PRECISION,
    max_value DOUBLE PRECISION,
    avg_value DOUBLE PRECISION,
    count INT DEFAULT 0,

    UNIQUE(sensor_uuid, date)
);



-- Indexes

CREATE INDEX IF NOT EXISTS idx_station_dates_date
    ON station_dates(date);

CREATE INDEX IF NOT EXISTS idx_sensor_files_date
    ON sensor_files(date);

CREATE INDEX IF NOT EXISTS idx_readings_date
    ON readings(date);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_date
    ON readings(sensor_uuid, date);

CREATE INDEX IF NOT EXISTS idx_readings_station_date
    ON readings(station_uuid, date);

CREATE INDEX IF NOT EXISTS idx_stations_box_id
    ON stations(box_id);

CREATE INDEX IF NOT EXISTS idx_stations_country
    ON stations(country);

CREATE INDEX IF NOT EXISTS idx_stations_region
    ON stations(country, region);

CREATE INDEX IF NOT EXISTS idx_stations_location
    ON stations USING GIST (location);

CREATE INDEX IF NOT EXISTS idx_sensors_sensor_id
    ON sensors(sensor_id);

CREATE INDEX IF NOT EXISTS idx_sensors_station
    ON sensors(station_uuid);

CREATE INDEX IF NOT EXISTS idx_sensors_category
    ON sensors(category);
