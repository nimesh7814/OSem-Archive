-- =============================================================
-- Initialization script for osem_db
-- Runs automatically on first container start
-- =============================================================

-- Enable extensions (both available in timescaledb-ha image)
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;

-- =============================================================
-- Table 1: stations
-- =============================================================
CREATE TABLE IF NOT EXISTS stations (
    st_uuid     INT                   GENERATED ALWAYS AS IDENTITY,
    st_id       VARCHAR(24)           NOT NULL,
    boxtype     TEXT,
    model       TEXT,
    geometry    GEOMETRY(Point, 4326) NOT NULL,
    region      TEXT,
    country     TEXT,

    CONSTRAINT pk_stations       PRIMARY KEY (st_uuid),
    CONSTRAINT uq_stations_st_id UNIQUE      (st_id)
);

CREATE INDEX IF NOT EXISTS sx_stations_geometry
    ON stations USING GIST (geometry);

-- =============================================================
-- Table 2: sensors
-- =============================================================
CREATE TABLE IF NOT EXISTS sensors (
    se_uuid     INT          GENERATED ALWAYS AS IDENTITY,
    se_id       VARCHAR(24)  NOT NULL,
    st_uuid     INT          NOT NULL,
    title       TEXT,
    unit        TEXT,
    info        TEXT,
    type        TEXT,

    CONSTRAINT pk_sensors        PRIMARY KEY (se_uuid),
    CONSTRAINT uq_sensors_se_id  UNIQUE      (se_id),
    CONSTRAINT fk_sensors_st_uuid
        FOREIGN KEY (st_uuid)
        REFERENCES stations (st_uuid)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_sensors_st_uuid
    ON sensors (st_uuid);

-- =============================================================
-- Table 3: readings (TimescaleDB hypertable)
-- =============================================================
CREATE TABLE IF NOT EXISTS readings (
    se_uuid     INT              NOT NULL,
    time        TIMESTAMPTZ      NOT NULL,
    value       DOUBLE PRECISION NOT NULL,

    CONSTRAINT fk_readings_se_uuid
        FOREIGN KEY (se_uuid)
        REFERENCES sensors (se_uuid)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_readings_se_uuid_time
    ON readings (se_uuid, time DESC);

-- Convert to TimescaleDB hypertable
SELECT create_hypertable('readings', 'time', if_not_exists => TRUE);
