DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        BEGIN
            EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_cron';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Skipping pg_cron creation: %', SQLERRM;
        END;
    ELSE
        RAISE NOTICE 'pg_cron not available on this server; skipping.';
    END IF;
END$$;


-- Ensure compression policies exist for ALL hypertables
CREATE OR REPLACE FUNCTION ensure_compression_policies_for_all_hypertables()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    r      RECORD;
    fqname TEXT;
BEGIN
    FOR r IN
        SELECT hypertable_schema, hypertable_name
        FROM timescaledb_information.hypertables
    LOOP
        fqname := format('%I.%I', r.hypertable_schema, r.hypertable_name);
        BEGIN
            EXECUTE format(
                $stmt$
                    SELECT add_compression_policy(
                        %L,
                        compress_after => INTERVAL '1 month',
                        if_not_exists  => TRUE
                    );
                $stmt$,
                fqname
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Could not set compression policy for %: %', fqname, SQLERRM;
        END;
    END LOOP;
END;
$$;


-- Refresh summary_table (country/region station+sensor+reading counts)
-- Counts readings directly from the readings table for accuracy.
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
        COUNT(DISTINCT st.st_uuid)        AS stations,
        COUNT(DISTINCT se.se_uuid)        AS sensors,
        COUNT(r.time)                     AS readings,
        now()                             AS refreshed_at
    FROM stations st
    LEFT JOIN sensors se ON se.st_uuid = st.st_uuid
    LEFT JOIN readings r  ON r.se_uuid  = se.se_uuid
    GROUP BY st.country, st.region;
END;
$$;


-- Refresh sensors-by-year breakdown
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


CREATE TABLE IF NOT EXISTS sensors_by_year_region_country (
    id           INT  GENERATED ALWAYS AS IDENTITY,
    year         INT,
    region       TEXT,
    country      TEXT,
    sensor_count BIGINT NOT NULL DEFAULT 0,

    CONSTRAINT pk_sensors_by_year PRIMARY KEY (id),
    CONSTRAINT uq_sensors_by_year UNIQUE NULLS NOT DISTINCT (year, region, country)
);


SELECT add_continuous_aggregate_policy('readings_hourly',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy('readings_daily',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy('readings_monthly',
    start_offset      => INTERVAL '3 months',
    end_offset        => INTERVAL '1 month',
    schedule_interval => INTERVAL '1 month',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy('readings_yearly',
    start_offset      => INTERVAL '3 years',
    end_offset        => INTERVAL '1 year',
    schedule_interval => INTERVAL '1 year',
    if_not_exists     => TRUE
);


SELECT add_compression_policy(
    'readings',
    compress_after => INTERVAL '1 month',
    if_not_exists  => TRUE
);


DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'cron') THEN

        -- Remove existing jobs to avoid duplicates on re-run
        DELETE FROM cron.job WHERE jobname IN (
            'compress-readings',
            'ensure-compression-policy-readings',
            'ensure-compression-policies-all',
            'refresh-summary-table',
            'refresh-sensors-by-year-region-country'
        );

        -- Compress chunks older than 1 month, twice a day
        PERFORM cron.schedule(
            'compress-readings',
            '0 */12 * * *',
            $cron$
                SELECT compress_chunk(i, if_not_compressed => TRUE)
                FROM show_chunks('readings', older_than => INTERVAL '1 month') i;
            $cron$
        );

        -- Re-register compression policy for readings daily (safety guard)
        PERFORM cron.schedule(
            'ensure-compression-policy-readings',
            '0 4 * * *',
            $cron$
                SELECT add_compression_policy(
                    'readings',
                    compress_after => INTERVAL '1 month',
                    if_not_exists  => TRUE
                );
            $cron$
        );

        -- Ensure compression policies exist for ALL hypertables (safety guard)
        PERFORM cron.schedule(
            'ensure-compression-policies-all',
            '5 4 * * *',
            $cron$
                SELECT ensure_compression_policies_for_all_hypertables();
            $cron$
        );

        -- Refresh summary_table every hour at :05
        PERFORM cron.schedule(
            'refresh-summary-table',
            '5 * * * *',
            $cron$
                SELECT refresh_summary_table();
            $cron$
        );

        -- Refresh sensors-by-year every hour at :10
        PERFORM cron.schedule(
            'refresh-sensors-by-year-region-country',
            '10 * * * *',
            $cron$
                SELECT refresh_sensors_by_year_region_country();
            $cron$
        );

        RAISE NOTICE 'All cron jobs registered successfully.';
    ELSE
        RAISE NOTICE 'cron schema not present; skipping cron.schedule registrations.';
    END IF;
END$$;


SELECT refresh_summary_table();
SELECT refresh_sensors_by_year_region_country();