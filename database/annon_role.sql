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

-- Grant read-only access to all current and future tables/sequences in
-- the `public` schema. Using the `ALL` forms is idempotent and avoids
-- errors when individual tables don't yet exist.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO web_anon;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO web_anon;

-- Ensure future tables and sequences created in `public` also grant
-- appropriate privileges to `web_anon`.
DO $$
BEGIN
    -- Default privileges require a role context; set for the current DB owner
    PERFORM 1;
EXCEPTION WHEN OTHERS THEN
    NULL;
END$$;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO web_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO web_anon;
