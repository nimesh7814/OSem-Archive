-- Create summary table if it doesn't exist, enforce singleton row
CREATE TABLE IF NOT EXISTS summary (
    id BIGINT PRIMARY KEY DEFAULT 1,
    stations BIGINT,
    sensors BIGINT,
    readings BIGINT,
    countries BIGINT,
    regions BIGINT,
    last_updated TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);

-- Create or replace the refresh function
CREATE OR REPLACE FUNCTION refresh_summary()
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO summary (id, stations, sensors, readings, countries, regions, last_updated)
    SELECT
        1,
        (SELECT COUNT(*) FROM stations WHERE country IS NOT NULL),
        (SELECT COUNT(*) FROM sensors se
            INNER JOIN stations st ON se.st_id = st.st_id
            WHERE st.country IS NOT NULL),
        (SELECT COALESCE(SUM(re.count), 0)
            FROM readings re
            INNER JOIN stations st ON re.st_id = st.st_id
            WHERE st.country IS NOT NULL),
        (SELECT COUNT(DISTINCT country) FROM stations WHERE country IS NOT NULL),
        (SELECT COUNT(DISTINCT region) FROM stations WHERE country IS NOT NULL),
        now()
    ON CONFLICT (id) DO UPDATE SET
        stations = EXCLUDED.stations,
        sensors = EXCLUDED.sensors,
        readings = EXCLUDED.readings,
        countries = EXCLUDED.countries,
        regions = EXCLUDED.regions,
        last_updated = EXCLUDED.last_updated;
END;
$$;

SELECT refresh_summary();

-- Cron Job
SELECT cron.unschedule(jobid)
FROM cron.job
WHERE jobname = 'refresh-summary';

SELECT cron.schedule(
    'refresh-summary',
    '0 0 * * *',
    'SELECT refresh_summary()'
);