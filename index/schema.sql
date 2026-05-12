-- ═══════════════════════════════════════════════════════════════════════════
-- OpenSenseMap Archive — PostgreSQL / PostGIS schema
-- ═══════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "postgis";
CREATE EXTENSION IF NOT EXISTS "pg_cron";


-- ─────────────────────────────────────────────────────────────────────────────
-- Admin boundaries  (loaded once from data/admin_boundary.geojson)
--
-- adm0_name  → country  name  (e.g. "Germany")
-- adm1_name  → region / state name  (e.g. "Bavaria")
--
-- Populated by running:
--   ogr2ogr -f PostgreSQL \
--     PG:"host=… dbname=opensensemap user=… password=…" \
--     data/admin_boundary.geojson \
--     -nln admin_boundaries \
--     -nlt MULTIPOLYGON \
--     -t_srs EPSG:4326 \
--     -overwrite
--
-- Or via the seed helper in indexer.py:
--   python indexer.py --mode seed-boundaries
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admin_boundaries (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    adm0_name  TEXT,                            -- country  name
    adm1_name  TEXT,                            -- region / state name
    geom       GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_admin_boundaries_geom
    ON admin_boundaries USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_admin_boundaries_adm0
    ON admin_boundaries (adm0_name);

CREATE INDEX IF NOT EXISTS idx_admin_boundaries_adm1
    ON admin_boundaries (adm0_name, adm1_name);


-- ─────────────────────────────────────────────────────────────────────────────
-- Index log  (one row per calendar date)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS index_log (
    uuid          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date          DATE NOT NULL UNIQUE,
    indexed_at    TIMESTAMPTZ DEFAULT now(),
    station_count INT DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',

    CONSTRAINT valid_status CHECK (
        status IN ('pending', 'success', 'partial', 'failed')
    )
);


-- ─────────────────────────────────────────────────────────────────────────────
-- Stations
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stations (
    uuid       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    box_id     TEXT NOT NULL UNIQUE,
    name       TEXT,
    location   GEOMETRY(POINT, 4326) DEFAULT NULL,
    country    TEXT,           -- from admin_boundaries.adm0_name
    region     TEXT,           -- from admin_boundaries.adm1_name
    city       TEXT,           -- best-effort: kept for backwards compat
    fs_date    DATE,           -- first seen date
    ls_date    DATE,           -- last  seen date
    init_date  TIMESTAMPTZ DEFAULT now()
);


-- ─────────────────────────────────────────────────────────────────────────────
-- Station dates  (one row per station × calendar date)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS station_dates (
    uuid         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    date         DATE NOT NULL,
    folder_url   TEXT,

    UNIQUE(station_uuid, date)
);


-- ─────────────────────────────────────────────────────────────────────────────
-- Sensors
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sensors (
    uuid         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_id    TEXT NOT NULL UNIQUE,
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    title        TEXT,
    type         TEXT,
    category     TEXT,
    unit         TEXT,
    init_date    TIMESTAMPTZ DEFAULT now()
);


-- ─────────────────────────────────────────────────────────────────────────────
-- Sensor files  (CSV URL per sensor × date)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sensor_files (
    uuid         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_uuid  UUID NOT NULL REFERENCES sensors(uuid) ON DELETE CASCADE,
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    date         DATE NOT NULL,
    csv_url      TEXT NOT NULL,

    UNIQUE(sensor_uuid, date)
);


-- ─────────────────────────────────────────────────────────────────────────────
-- Readings  (daily aggregates per sensor)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS readings (
    uuid         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sensor_uuid  UUID NOT NULL REFERENCES sensors(uuid) ON DELETE CASCADE,
    station_uuid UUID NOT NULL REFERENCES stations(uuid) ON DELETE CASCADE,
    date         DATE NOT NULL,
    recorded_at  TIMESTAMPTZ,
    min_value    DOUBLE PRECISION,
    max_value    DOUBLE PRECISION,
    avg_value    DOUBLE PRECISION,
    count        INT DEFAULT 0,

    UNIQUE(sensor_uuid, date)
);


-- ─────────────────────────────────────────────────────────────────────────────
-- Indexes
-- ─────────────────────────────────────────────────────────────────────────────

-- station_dates
CREATE INDEX IF NOT EXISTS idx_station_dates_date
    ON station_dates(date);

-- sensor_files
CREATE INDEX IF NOT EXISTS idx_sensor_files_date
    ON sensor_files(date);

-- readings
CREATE INDEX IF NOT EXISTS idx_readings_date
    ON readings(date);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_date
    ON readings(sensor_uuid, date);

CREATE INDEX IF NOT EXISTS idx_readings_station_date
    ON readings(station_uuid, date);

-- stations  (fixed: was referencing non-existent column country_code)
CREATE INDEX IF NOT EXISTS idx_stations_box_id
    ON stations(box_id);

CREATE INDEX IF NOT EXISTS idx_stations_country
    ON stations(country);

CREATE INDEX IF NOT EXISTS idx_stations_region
    ON stations(country, region);

-- spatial index on station point geometry
CREATE INDEX IF NOT EXISTS idx_stations_location
    ON stations USING GIST (location);

-- sensors
CREATE INDEX IF NOT EXISTS idx_sensors_sensor_id
    ON sensors(sensor_id);

CREATE INDEX IF NOT EXISTS idx_sensors_station
    ON sensors(station_uuid);

CREATE INDEX IF NOT EXISTS idx_sensors_category
    ON sensors(category);
