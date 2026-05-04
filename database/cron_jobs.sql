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


SELECT cron.schedule(
    'compress-readings',               
    '0 */12 * * *',                  
    $$
        SELECT add_compression_policy(
            'readings',
            compress_after  => INTERVAL '1 month',
            if_not_exists   => TRUE
        );
    $$
);


SELECT cron.schedule(
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

SELECT cron.schedule(
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

SELECT cron.schedule(
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

SELECT cron.schedule(
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


SELECT cron.schedule(
    'refresh-summary-table',        
    '5 * * * *',                        
    $$
        SELECT refresh_summary_table();
    $$
);


SELECT refresh_summary_table();