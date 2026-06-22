-- ============================================================
-- restore.sql
-- Run this AFTER load_data.py completes successfully
-- ============================================================

-- Step 1: Re-enable autovacuum on all tables
ALTER TABLE readings SET (autovacuum_enabled = true);
ALTER TABLE sensors SET (autovacuum_enabled = true);
ALTER TABLE stations SET (autovacuum_enabled = true);

-- Step 2: Restore FK constraint on readings
ALTER TABLE readings ADD CONSTRAINT fk_readings_se_id
    FOREIGN KEY (se_id) REFERENCES sensors(se_id)
    ON UPDATE CASCADE ON DELETE CASCADE;

-- Step 3: Vacuum and analyze all tables
VACUUM ANALYZE readings;
VACUUM ANALYZE sensors;
VACUUM ANALYZE stations;

-- Step 4: Restore compression policy (1 day)
SELECT remove_compression_policy('readings');
SELECT add_compression_policy('readings', compress_after => INTERVAL '1 day', if_not_exists => TRUE);

-- Step 4b: Compress all existing historical chunks immediately
-- (the policy only applies to new data going forward)
SELECT compress_chunk(chunk)
FROM show_chunks('readings') AS chunk;

-- Step 5: Refresh continuous aggregates
CALL refresh_continuous_aggregate('readings_daily', NULL, NULL);
CALL refresh_continuous_aggregate('readings_monthly', NULL, NULL);
CALL refresh_continuous_aggregate('readings_yearly', NULL, NULL);