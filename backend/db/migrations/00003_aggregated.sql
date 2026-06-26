-- 00003_continuous_aggregates.sql
-- Hierarchical TimescaleDB continuous aggregates for measurements:
-- hourly -> daily -> monthly -> yearly. Each tier rolls up from the one
-- below it rather than re-scanning raw measurements, which matters once
-- the raw table holds billions of rows.


BEGIN;

-- Hourly (reads from raw measurements — value column exists here)
CREATE MATERIALIZED VIEW reading_hourly
WITH (timescaledb.continuous, timescaledb.materialized_only = true) AS
SELECT
    sensor_id,
    time_bucket('1 hour', time) AS bucket,
    sum(value) AS sum_value,
    count(value) AS rdgs_count,
    avg(value) AS avg_value,
    min(value) AS min_value,
    max(value) AS max_value
FROM measurements
GROUP BY sensor_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('reading_hourly',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour'
);

CREATE INDEX idx_reading_hourly_sensor_bucket
    ON reading_hourly (sensor_id, bucket DESC);

-- Daily (rolls up from reading_hourly)
CREATE MATERIALIZED VIEW reading_daily
WITH (timescaledb.continuous, timescaledb.materialized_only = true) AS
SELECT
    sensor_id,
    time_bucket('1 day', bucket) AS bucket,
    sum(sum_value) AS sum_value,
    sum(rdgs_count) AS rdgs_count,
    avg(avg_value) AS avg_value,
    min(min_value) AS min_value,
    max(max_value) AS max_value
FROM reading_hourly
GROUP BY sensor_id, time_bucket('1 day', bucket)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('reading_daily',
    start_offset => INTERVAL '10 days',
    end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day'
);

CREATE INDEX idx_reading_daily_sensor_bucket
    ON reading_daily (sensor_id, bucket DESC);

-- Monthly (rolls up from reading_daily)
CREATE MATERIALIZED VIEW reading_monthly
WITH (timescaledb.continuous, timescaledb.materialized_only = true) AS
SELECT
    sensor_id,
    time_bucket('1 month', bucket) AS bucket,
    sum(sum_value) AS sum_value,
    sum(rdgs_count) AS rdgs_count,
    avg(avg_value) AS avg_value,
    min(min_value) AS min_value,
    max(max_value) AS max_value
FROM reading_daily
GROUP BY sensor_id, time_bucket('1 month', bucket)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('reading_monthly',
    start_offset => INTERVAL '3 months',
    end_offset => INTERVAL '1 month',
    schedule_interval => INTERVAL '1 day'
);

CREATE INDEX idx_reading_monthly_sensor_bucket
    ON reading_monthly (sensor_id, bucket DESC);

-- Yearly (rolls up from reading_monthly)
CREATE MATERIALIZED VIEW reading_yearly
WITH (timescaledb.continuous, timescaledb.materialized_only = true) AS
SELECT
    sensor_id,
    time_bucket('1 year', bucket) AS bucket,
    sum(sum_value) AS sum_value,
    sum(rdgs_count) AS rdgs_count,
    avg(avg_value) AS avg_value,
    min(min_value) AS min_value,
    max(max_value) AS max_value
FROM reading_monthly
GROUP BY sensor_id, time_bucket('1 year', bucket)
WITH NO DATA;

SELECT add_continuous_aggregate_policy('reading_yearly',
    start_offset => INTERVAL '3 years',
    end_offset => INTERVAL '1 year',
    schedule_interval => INTERVAL '1 day'
);

CREATE INDEX idx_reading_yearly_sensor_bucket
    ON reading_yearly (sensor_id, bucket DESC);

COMMIT;


-- -----------------------------------------------------------------------------
-- Initial backfill — populate all four views with existing historical data.
-- Continuous aggregates created WITH NO DATA are empty until either the
-- refresh policy runs on schedule, or you manually refresh once like this.
-- Run OUTSIDE the transaction above (CALL cannot run inside BEGIN/COMMIT).
-- -----------------------------------------------------------------------------

-- CALL refresh_continuous_aggregate('reading_hourly', NULL, NULL);
-- CALL refresh_continuous_aggregate('reading_daily', NULL, NULL);
-- CALL refresh_continuous_aggregate('reading_monthly', NULL, NULL);
-- CALL refresh_continuous_aggregate('reading_yearly', NULL, NULL);
