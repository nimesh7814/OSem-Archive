-- 00004_export_job.sql
-- export_job was created with a narrow, single-purpose shape (box_id,
-- phenomenon, date_from, date_to) that the API never actually used. The API
-- instead needs an arbitrary filter set (region or AOI, exposure, phenomenon,
-- sensor_type, box_ids, date range) plus which aggregate tier to read from,
-- so those filters move into a single JSONB column and a dedicated
-- aggregate column is added. format's check is widened since the API writes
-- 'geojson', not 'json'.

BEGIN;

CREATE TABLE IF NOT EXISTS export_job (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    format TEXT NOT NULL DEFAULT 'csv',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    row_count INT,
    file_url TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    aggregate TEXT NOT NULL DEFAULT 'hourly',
    filters JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE export_job
    DROP COLUMN IF EXISTS box_id,
    DROP COLUMN IF EXISTS phenomenon,
    DROP COLUMN IF EXISTS date_from,
    DROP COLUMN IF EXISTS date_to;

ALTER TABLE export_job
    ADD COLUMN IF NOT EXISTS aggregate TEXT NOT NULL DEFAULT 'hourly',
    ADD COLUMN IF NOT EXISTS filters JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE export_job
    DROP CONSTRAINT IF EXISTS export_job_format_check,
    DROP CONSTRAINT IF EXISTS export_job_aggregate_check,
    ADD CONSTRAINT export_job_aggregate_check
        CHECK (aggregate IN ('raw', 'hourly', 'daily', 'monthly', 'yearly')),
    ADD CONSTRAINT export_job_format_check CHECK (format IN ('csv', 'geojson'));

CREATE INDEX IF NOT EXISTS idx_export_job_status ON export_job (status);

COMMIT;
