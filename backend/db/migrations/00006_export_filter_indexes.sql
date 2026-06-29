-- Support API filters used by region/AOI station queries and export jobs.

CREATE INDEX IF NOT EXISTS idx_regions_region
ON regions (region);

CREATE INDEX IF NOT EXISTS idx_sensors_sensor_type_id
ON sensors (sensor_type, id);

CREATE INDEX IF NOT EXISTS idx_sensors_title_id
ON sensors (title, id);

CREATE INDEX IF NOT EXISTS idx_export_job_created_at
ON export_job (created_at DESC);
