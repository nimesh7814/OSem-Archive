-- annon_role.sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'web_anon') THEN
        CREATE ROLE web_anon NOLOGIN;
    END IF;
END
$$;


DO $$
DECLARE
    db_owner TEXT;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(d.datdba)
    INTO db_owner
    FROM pg_catalog.pg_database d
    WHERE d.datname = current_database();

    EXECUTE format('GRANT web_anon TO %I', db_owner);
END
$$;


GRANT USAGE ON SCHEMA public TO web_anon;


GRANT SELECT ON TABLE stations      TO web_anon;
GRANT SELECT ON TABLE sensors       TO web_anon;
GRANT SELECT ON TABLE readings      TO web_anon;
GRANT SELECT ON TABLE summary_table TO web_anon;
GRANT SELECT ON TABLE downloads     TO web_anon;

GRANT SELECT ON TABLE readings_hourly   TO web_anon;
GRANT SELECT ON TABLE readings_daily    TO web_anon;
GRANT SELECT ON TABLE readings_monthly  TO web_anon;
GRANT SELECT ON TABLE readings_yearly   TO web_anon;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO web_anon;
