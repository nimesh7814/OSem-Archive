-- 00008_export_job_expiry.sql
-- Export files are auto-deleted 24h after completion; this records when.

ALTER TABLE export_job
    ADD COLUMN expires_at TIMESTAMPTZ;
