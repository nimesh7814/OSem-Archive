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


CREATE OR REPLACE FUNCTION refresh_summary_table()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    r RECORD;
BEGIN
    TRUNCATE summary_table RESTART IDENTITY;

    FOR r IN
        SELECT DISTINCT st.country, st.region
        FROM stations st
        ORDER BY st.country, st.region
    LOOP
        INSERT INTO summary_table (country, region, stations, sensors, readings, refreshed_at)
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_uuid)  AS stations,
            COUNT(DISTINCT se.se_uuid)  AS sensors,
            COUNT(r2.time)              AS readings,
            now()                       AS refreshed_at
        FROM stations st
        LEFT JOIN sensors se ON se.st_uuid = st.st_uuid
        LEFT JOIN readings r2 ON r2.se_uuid = se.se_uuid
        WHERE (st.country = r.country OR (st.country IS NULL AND r.country IS NULL))
          AND (st.region  = r.region  OR (st.region  IS NULL AND r.region  IS NULL))
        GROUP BY st.country, st.region
        ON CONFLICT ON CONSTRAINT uq_summary_country_region
        DO UPDATE SET
            stations     = EXCLUDED.stations,
            sensors      = EXCLUDED.sensors,
            readings     = EXCLUDED.readings,
            refreshed_at = EXCLUDED.refreshed_at;
    END LOOP;
END;
$$;


-- Incremental refresh of summary_table
CREATE OR REPLACE FUNCTION incremental_refresh_summary_table()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    last_refresh TIMESTAMPTZ;
    r            RECORD;
BEGIN
    -- Get the last refresh timestamp from summary_table
    -- If summary_table is empty, fall back to full refresh
    SELECT MIN(refreshed_at) INTO last_refresh FROM summary_table;

    IF last_refresh IS NULL THEN
        RAISE NOTICE 'summary_table is empty, running full refresh...';
        PERFORM refresh_summary_table();
        RETURN;
    END IF;

    RAISE NOTICE 'Incremental refresh from %', last_refresh;

    -- Update reading counts for country/region combinations
    -- that have new readings since last refresh
    FOR r IN
        SELECT DISTINCT st.country, st.region
        FROM stations st
        JOIN sensors se ON se.st_uuid = st.st_uuid
        JOIN readings rd ON rd.se_uuid = se.se_uuid
        WHERE rd.time > last_refresh
        ORDER BY st.country, st.region
    LOOP
        -- Count only new readings since last refresh for this country/region
        INSERT INTO summary_table (country, region, stations, sensors, readings, refreshed_at)
        SELECT
            st.country,
            st.region,
            COUNT(DISTINCT st.st_uuid)                           AS stations,
            COUNT(DISTINCT se.se_uuid)                           AS sensors,
            COALESCE(
                (SELECT readings FROM summary_table s
                 WHERE (s.country = st.country OR (s.country IS NULL AND st.country IS NULL))
                   AND (s.region  = st.region  OR (s.region  IS NULL AND st.region  IS NULL))
                ), 0
            ) + COUNT(r2.time)                                   AS readings,
            now()                                                AS refreshed_at
        FROM stations st
        LEFT JOIN sensors se ON se.st_uuid = st.st_uuid
        LEFT JOIN readings r2 ON r2.se_uuid = se.se_uuid
            AND r2.time > last_refresh
        WHERE (st.country = r.country OR (st.country IS NULL AND r.country IS NULL))
          AND (st.region  = r.region  OR (st.region  IS NULL AND r.region  IS NULL))
        GROUP BY st.country, st.region
        ON CONFLICT ON CONSTRAINT uq_summary_country_region
        DO UPDATE SET
            stations     = EXCLUDED.stations,
            sensors      = EXCLUDED.sensors,
            readings     = EXCLUDED.readings,
            refreshed_at = EXCLUDED.refreshed_at;
    END LOOP;

    -- Also handle newly added stations/sensors that may have no readings yet.
    -- Uses IS NOT DISTINCT FROM for NULL-safe equality so PostgreSQL can
    -- plan a hash/merge join (avoiding the "FULL JOIN" planner error).
    INSERT INTO summary_table (country, region, stations, sensors, readings, refreshed_at)
    SELECT
        st.country,
        st.region,
        COUNT(DISTINCT st.st_uuid) AS stations,
        COUNT(DISTINCT se.se_uuid) AS sensors,
        -- Preserve any existing reading count; don't overwrite with 0
        COALESCE(
            (SELECT s.readings FROM summary_table s
             WHERE s.country IS NOT DISTINCT FROM st.country
               AND s.region  IS NOT DISTINCT FROM st.region
            ), 0
        )                          AS readings,
        now()                      AS refreshed_at
    FROM stations st
    LEFT JOIN sensors se ON se.st_uuid = st.st_uuid
    WHERE st.init_date > last_refresh
    GROUP BY st.country, st.region
    ON CONFLICT ON CONSTRAINT uq_summary_country_region
    DO UPDATE SET
        stations     = EXCLUDED.stations,
        sensors      = EXCLUDED.sensors,
        -- Keep the greater reading count to avoid overwriting real data with 0
        readings     = GREATEST(summary_table.readings, EXCLUDED.readings),
        refreshed_at = EXCLUDED.refreshed_at;

END;
$$;


-- Refresh sensors-by-year breakdown (incremental)
-- Only adds newly registered sensors since last refresh.
CREATE OR REPLACE FUNCTION refresh_sensors_by_year_region_country()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    -- Check if table is empty → full refresh, otherwise incremental upsert
    PERFORM 1 FROM sensors_by_year_region_country LIMIT 1;

    IF NOT FOUND THEN
        -- Full refresh on first run
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
        ON CONFLICT ON CONSTRAINT uq_sensors_by_year
        DO UPDATE SET sensor_count = EXCLUDED.sensor_count;
        RETURN;
    END IF;

    -- Incremental: upsert all current counts (ON CONFLICT handles updates)
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
    ON CONFLICT ON CONSTRAINT uq_sensors_by_year
    DO UPDATE SET sensor_count = EXCLUDED.sensor_count;
END;
$$;


CREATE TABLE IF NOT EXISTS sensors_by_year_region_country (
    id           INT  GENERATED ALWAYS AS IDENTITY,
    year         INT,
    region       TEXT,
    country      TEXT,
    sensor_count BIGINT NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT pk_sensors_by_year PRIMARY KEY (id),
    CONSTRAINT uq_sensors_by_year UNIQUE NULLS NOT DISTINCT (year, region, country)
);


DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'cron') THEN

        -- Remove existing jobs to avoid duplicates on re-run
        DELETE FROM cron.job WHERE jobname IN (
            'refresh-summary-table',
            'refresh-sensors-by-year-region-country'
        );

        -- Incremental refresh of summary_table every night at midnight
        PERFORM cron.schedule(
            'refresh-summary-table',
            '0 0 * * *',
            $cron$
                SELECT incremental_refresh_summary_table();
            $cron$
        );

        -- Refresh sensors-by-year every night at midnight
        PERFORM cron.schedule(
            'refresh-sensors-by-year-region-country',
            '0 0 * * *',
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