-- Run interactively in psql as a Ketoshop database administrator.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ketoshop_diagnostics') THEN
        CREATE ROLE ketoshop_diagnostics
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
    END IF;
END
$role$;
\password ketoshop_diagnostics

SELECT current_database() AS database_name \gset
GRANT CONNECT ON DATABASE :"database_name" TO ketoshop_diagnostics;

-- Remove all direct table and
-- sequence rights from this role before granting the two diagnostic views.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ketoshop_diagnostics;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ketoshop_diagnostics;
GRANT USAGE ON SCHEMA public TO ketoshop_diagnostics;
