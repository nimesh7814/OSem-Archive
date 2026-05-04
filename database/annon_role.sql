-- annon_role.sql
-- --------------------------
-- Creates the `web_anon` role used by PostgREST for all unauthenticated
-- (anonymous) requests and grants it read-only access to every table,
-- view, and materialized view that should be publicly queryable.
--
-- Run once after schema.sql:
--     python load_sql.py annon_role.sql

-- ---------------------------------------------------------------------------
-- Role
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'web_anon') THEN
        CREATE ROLE web_anon NOLOGIN;
    END IF;
END
$$;

-- Allow PostgREST's authenticator user to switch into web_anon.
-- (PostgREST uses SET ROLE internally for every request.)
-- Explicitly names the DB owner rather than relying on current_user,
-- so this is safe to re-run as any superuser.
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

-- ---------------------------------------------------------------------------
-- Schema usage
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO web_anon;

-- ---------------------------------------------------------------------------
-- Tables  (read-only SELECT)
-- ---------------------------------------------------------------------------
GRANT SELECT ON TABLE stations      TO web_anon;
GRANT SELECT ON TABLE sensors       TO web_anon;
GRANT SELECT ON TABLE readings      TO web_anon;
GRANT SELECT ON TABLE summary_table TO web_anon;
GRANT SELECT ON TABLE downloads     TO web_anon;

-- ---------------------------------------------------------------------------
-- Continuous aggregate materialized views  (read-only SELECT)
-- ---------------------------------------------------------------------------
GRANT SELECT ON TABLE readings_hourly   TO web_anon;
GRANT SELECT ON TABLE readings_daily    TO web_anon;
GRANT SELECT ON TABLE readings_monthly  TO web_anon;
GRANT SELECT ON TABLE readings_yearly   TO web_anon;

-- ---------------------------------------------------------------------------
-- Sequences  (needed so PostgREST can introspect identity columns)
-- ---------------------------------------------------------------------------
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO web_anon;
