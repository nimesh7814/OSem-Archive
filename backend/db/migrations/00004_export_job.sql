-- 00004_export_job.sql
-- export_job was created with a narrow, single-purpose shape (box_id,
-- phenomenon, date_from, date_to) that the API never actually used. The API
-- instead needs an arbitrary filter set (region or AOI, exposure, phenomenon,
-- sensor_type, box_ids, date range) plus which aggregate tier to read from,
-- so those filters move into a single JSONB column and a dedicated
-- aggregate column is added. format's check is widened since the API writes
-- 'geojson', not 'json'.

BEGIN;

ALTER TABLE export_job
    DROP COLUMN box_id,
    DROP COLUMN phenomenon,
    DROP COLUMN date_from,
    DROP COLUMN date_to;

ALTER TABLE export_job
    ADD COLUMN aggregate TEXT NOT NULL DEFAULT 'hourly'
        CHECK (aggregate IN ('raw', 'hourly', 'daily', 'monthly', 'yearly')),
    ADD COLUMN filters JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE export_job
    DROP CONSTRAINT export_job_format_check,
    ADD CONSTRAINT export_job_format_check CHECK (format IN ('csv', 'geojson'));

COMMIT;
