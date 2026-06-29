-- Speed up latest-measurement lookups for box sensor payloads.
CREATE INDEX IF NOT EXISTS idx_measurements_sensor_time
ON measurements (sensor_id, time DESC);

