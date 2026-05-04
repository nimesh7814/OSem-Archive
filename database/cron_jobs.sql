
-- Ensure pg_cron extension exists where available. Moved from `schema.sql`.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        BEGIN
            EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_cron';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Skipping pg_cron creation: %', SQLERRM;
        END;
    ELSE
        RAISE NOTICE 'pg_cron not available on this server; skipping pg_cron creation.';
    END IF;
END$$;

CREATE OR REPLACE FUNCTION refresh_summary_table()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN

    TRUNCATE summary_table RESTART IDENTITY;

    INSERT INTO summary_table (country, region, stations, sensors, readings, refreshed_at)
    SELECT
        st.country,
        st.region,
        COUNT(DISTINCT st.st_uuid)                          AS stations,
        COUNT(DISTINCT se.se_uuid)                          AS sensors,
        COALESCE(SUM(r.reading_count), 0)                   AS readings,
        now()                                               AS refreshed_at
    FROM stations st
    LEFT JOIN sensors  se ON se.st_uuid = st.st_uuid
    LEFT JOIN (
  
        SELECT
            se_uuid,
            COUNT(*) AS reading_count
        FROM readings
        GROUP BY se_uuid
    ) r ON r.se_uuid = se.se_uuid
    GROUP BY st.country, st.region;
END;
$$;

CREATE OR REPLACE FUNCTION refresh_sensors_by_year_region_country()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    TRUNCATE TABLE sensors_by_year_region_country RESTART IDENTITY;

    INSERT INTO sensors_by_year_region_country (year, region, country, sensor_count)
    SELECT
        EXTRACT(YEAR FROM s.init_date)::INT AS year,
        st.region,
        st.country,
        COUNT(*)                            AS sensor_count
    FROM sensors s
    JOIN stations st ON s.st_uuid = st.st_uuid
    WHERE s.init_date IS NOT NULL
    GROUP BY year, st.region, st.country
    ORDER BY year DESC, st.country, st.region;
END;
$$;


DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'cron') THEN
        PERFORM cron.schedule(
            'compress-readings',
            '0 */12 * * *',
            $$
                SELECT compress_chunk(i, if_not_compressed => TRUE)
                FROM show_chunks('readings', older_than => INTERVAL '1 month') i;
            $$
        );

        -- Daily ensure compression policy exists for `readings` (idempotent)
        PERFORM cron.schedule(
            'ensure-compression-policy-readings',
            '0 4 * * *',
            $$
                SELECT add_compression_policy('readings', compress_after => INTERVAL '1 month', if_not_exists => TRUE);
            $$
        );

        -- Schedule the function to run daily at 04:05 to reconcile policies across the DB.
        PERFORM cron.schedule(
            'ensure-compression-policies-all',
            '5 4 * * *',
            $$
                SELECT ensure_compression_policies_for_all_hypertables();
            $$
        );

        PERFORM cron.schedule(
            'cagg-policy-readings-hourly',
            '0 * * * *',
            $$
                SELECT add_continuous_aggregate_policy('readings_hourly',
                    start_offset      => INTERVAL '2 days',
                    end_offset        => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists     => TRUE
                );
            $$
        );

        PERFORM cron.schedule(
            'cagg-policy-readings-daily',
            '0 1 * * *',
            $$
                SELECT add_continuous_aggregate_policy('readings_daily',
                    start_offset      => INTERVAL '7 days',
                    end_offset        => INTERVAL '1 day',
                    schedule_interval => INTERVAL '1 day',
                    if_not_exists     => TRUE
                );
            $$
        );

        PERFORM cron.schedule(
            'cagg-policy-readings-monthly',
            '0 2 1 * *',
            $$
                SELECT add_continuous_aggregate_policy('readings_monthly',
                    start_offset      => INTERVAL '3 months',
                    end_offset        => INTERVAL '1 month',
                    schedule_interval => INTERVAL '1 month',
                    if_not_exists     => TRUE
                );
            $$
        );

        PERFORM cron.schedule(
            'cagg-policy-readings-yearly',
            '0 3 1 1 *',
            $$
                SELECT add_continuous_aggregate_policy('readings_yearly',
                    start_offset      => INTERVAL '3 years',
                    end_offset        => INTERVAL '1 year',
                    schedule_interval => INTERVAL '1 year',
                    if_not_exists     => TRUE
                );
            $$
        );

        PERFORM cron.schedule(
            'refresh-summary-table',
            '5 * * * *',
            $$
                SELECT refresh_summary_table();
            $$
        );

        PERFORM cron.schedule(
            'refresh-sensors-by-year-region-country',
            '10 * * * *',
            $$
                SELECT refresh_sensors_by_year_region_country();
            $$
        );
    ELSE
        RAISE NOTICE 'cron schema not present; skipping all cron.schedule registrations.';
    END IF;
END$$;


-- Ensure compression policies exist for all hypertables in the database.
-- This iterates over `timescaledb_information.hypertables` and applies a
-- sensible default compression policy when missing. It is idempotent.
CREATE OR REPLACE FUNCTION ensure_compression_policies_for_all_hypertables()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
    fqname TEXT;
BEGIN
    FOR r IN SELECT hypertable_schema, hypertable_name FROM timescaledb_information.hypertables LOOP
        fqname := format('%I.%I', r.hypertable_schema, r.hypertable_name);
        BEGIN
            EXECUTE format(
                $stmt$SELECT add_compression_policy(%L, compress_after => INTERVAL '1 month', if_not_exists => TRUE);$stmt$,
                fqname
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Could not ensure compression policy for %: %', fqname, SQLERRM;
        END;
    END LOOP;
END;
$$;

SELECT refresh_summary_table();
SELECT refresh_sensors_by_year_region_country();