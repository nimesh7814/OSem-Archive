-- ═══════════════════════════════════════════════════════════════
-- PostgREST roles
-- Runs after schema.sql (02_postgrest_roles.sql)
-- ═══════════════════════════════════════════════════════════════

-- ── Authenticator role (used by PostgREST to connect) ────────
-- This role logs in but has no direct table access; it switches
-- to web_anon (or other roles) for each request.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticator') THEN
    CREATE ROLE authenticator NOINHERIT LOGIN PASSWORD 'change_authenticator_password';
  END IF;
END
$$;

-- ── Anonymous / public read-only role ────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'web_anon') THEN
    CREATE ROLE web_anon NOLOGIN;
  END IF;
END
$$;

-- Allow authenticator to switch into web_anon
GRANT web_anon TO authenticator;

-- ── Read-only grants for web_anon ────────────────────────────
-- Expose the schema
GRANT USAGE ON SCHEMA public TO web_anon;

-- Tables (read-only)
GRANT SELECT ON TABLE public.stations      TO web_anon;
GRANT SELECT ON TABLE public.sensors       TO web_anon;
GRANT SELECT ON TABLE public.readings      TO web_anon;
GRANT SELECT ON TABLE public.sensor_files  TO web_anon;
GRANT SELECT ON TABLE public.station_dates TO web_anon;
GRANT SELECT ON TABLE public.index_log     TO web_anon;

-- Future tables created by the owner will also be selectable
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO web_anon;
