-- 00007_sensors_last_measurement.sql
-- Track each sensor's most recent reading directly on the sensors row,
-- avoiding a per-request LATERAL lookup into measurements.

BEGIN;

ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS last_measurement DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS last_timestamp TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_sensors_last_timestamp ON sensors (last_timestamp);

-- One-time backfill from already-ingested measurements. Guarded by
-- last_timestamp IS NULL so it's safe to re-run and skips sensors ingest
-- has already kept up to date (see ingest/load_archive.py).
UPDATE sensors s
SET (last_measurement, last_timestamp) = (
    SELECT m.value, m.time
    FROM measurements m
    WHERE m.sensor_id = s.id
    ORDER BY m.time DESC
    LIMIT 1
)
WHERE s.last_timestamp IS NULL
  AND EXISTS (SELECT 1 FROM measurements m WHERE m.sensor_id = s.id);

COMMIT;
