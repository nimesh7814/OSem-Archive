CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;

-- -----------------------------------------------------------
-- stations
-- -----------------------------------------------------------
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

CREATE INDEX IF NOT EXISTS sx_stations_geometry
    ON stations USING GIST (geometry);

-- -----------------------------------------------------------
-- sensors
-- -----------------------------------------------------------
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

-- -----------------------------------------------------------
-- readings
-- Unique constraint on (se_uuid, time) prevents duplicate
-- readings and enables ON CONFLICT DO NOTHING in load_data.py
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS readings (
    se_uuid     INT              NOT NULL,
    time        TIMESTAMPTZ      NOT NULL,
    value       DOUBLE PRECISION NOT NULL,

    CONSTRAINT pk_readings        PRIMARY KEY (se_uuid, time),
    CONSTRAINT fk_readings_se_uuid
        FOREIGN KEY (se_uuid)
        REFERENCES sensors (se_uuid)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

-- -----------------------------------------------------------
-- downloads
-- -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS downloads (
    dl_uuid     INT     GENERATED ALWAYS AS IDENTITY,
    type        TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT pk_downloads PRIMARY KEY (dl_uuid)
);
