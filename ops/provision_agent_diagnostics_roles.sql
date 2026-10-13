-- Create (or re-converge) the dedicated database role the diagnostics broker uses
-- for `db_sql`. Run once per project database as the Postgres admin:
--
--   psql -h 127.0.0.1 -U postgres -d <database> -v ON_ERROR_STOP=1 \
--     -v agent_role=<project>_agent -v table_owner=<application role> \
--     [-v app_schema=public] [-v readonly_tables=a,b] [-v hidden_columns=t.c,...] \
--     -f ops/provision_agent_diagnostics_roles.sql
--   psql -h 127.0.0.1 -U postgres -d <database> -c '\password <project>_agent'
--
-- agent_role      role to create, `<name>_agent` (e.g. ketoshop_agent, kans_shop_agent)
-- table_owner     the role that owns the application tables and runs migrations;
--                 default privileges are attached to it so future tables are covered
-- app_schema      schema of the application tables (default public)
-- readonly_tables comma list: SELECT only for these tables (alembic_version is
--                 always read-only); every other existing table gets full DML.
--                 Giving a list also makes FUTURE tables SELECT-only until the
--                 script is re-run (with the same full list) after a migration,
--                 which then grants DML to any table not named here.
-- hidden_columns  comma list of table.column: not readable at all (capabilities
--                 such as lease ids and invite token hashes)
--
-- The role is never a superuser, cannot create roles or databases, cannot
-- replicate, does not bypass row-level security, and is not a member of any
-- built-in pg_* role. The broker re-checks all of that on every connection and
-- refuses to run agent SQL otherwise (diagnostic_host.ROLE_GUARD_SQL).
--
-- Afterwards, stop the role reaching OTHER projects' databases (run once per
-- database; the owner/application roles keep access as database owner or via an
-- explicit grant):
--   REVOKE CONNECT ON DATABASE <other_db> FROM PUBLIC;
--   GRANT  CONNECT ON DATABASE <other_db> TO <that database's application role>;

\if :{?agent_role}
\else
  \echo 'pass -v agent_role=<project>_agent'
  \quit
\endif
\if :{?table_owner}
\else
  \echo 'pass -v table_owner=<application role that owns the tables>'
  \quit
\endif
\if :{?app_schema}
\else
  \set app_schema public
\endif
\if :{?readonly_tables}
\else
  \set readonly_tables ''
\endif
\if :{?hidden_columns}
\else
  \set hidden_columns ''
\endif

SELECT set_config('diag.agent_role', :'agent_role', false) AS a,
       set_config('diag.table_owner', :'table_owner', false) AS b,
       set_config('diag.schema', :'app_schema', false) AS c,
       set_config('diag.readonly', :'readonly_tables', false) AS d,
       set_config('diag.hidden', :'hidden_columns', false) AS e \gset

DO $provision$
DECLARE
    agent text := current_setting('diag.agent_role');
    owner_role text := current_setting('diag.table_owner');
    app_schema text := current_setting('diag.schema');
    database text := current_database();
    readonly text[] := ARRAY['alembic_version']
        || COALESCE(string_to_array(NULLIF(current_setting('diag.readonly'), ''), ','), '{}');
    hidden text[] := COALESCE(
        string_to_array(NULLIF(current_setting('diag.hidden'), ''), ','), '{}');
    restrictive boolean := cardinality(readonly) > 1;
    rel record;
    readable text;
    other text;
BEGIN
    IF agent !~ '^[a-z][a-z0-9_]*_agent$' THEN
        RAISE EXCEPTION 'agent_role must look like <project>_agent, got %', agent;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = owner_role) THEN
        RAISE EXCEPTION 'table_owner role % does not exist', owner_role;
    END IF;
    IF agent = owner_role THEN
        RAISE EXCEPTION 'the agent role must not be the application role';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = agent) THEN
        EXECUTE format('CREATE ROLE %I LOGIN', agent);
    END IF;
    EXECUTE format(
        'ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
        'NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8', agent);
    -- Limits only a superuser can set (the broker, not being one, cannot).
    EXECUTE format('ALTER ROLE %I SET temp_file_limit = ''256MB''', agent);
    EXECUTE format('ALTER ROLE %I SET statement_timeout = ''15s''', agent);

    -- No built-in role membership, whatever it was before.
    FOR other IN
        SELECT r.rolname FROM pg_auth_members m
        JOIN pg_roles r ON r.oid = m.roleid
        JOIN pg_roles u ON u.oid = m.member
        WHERE u.rolname = agent
    LOOP
        EXECUTE format('REVOKE %I FROM %I', other, agent);
    END LOOP;

    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', database, agent);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', app_schema, agent);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM %I', app_schema, agent);
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', app_schema, agent);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', app_schema, agent);

    FOR rel IN
        SELECT c.oid, c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = app_schema AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        -- SELECT on every column except the hidden ones.
        SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum) INTO readable
        FROM pg_attribute a
        WHERE a.attrelid = rel.oid AND a.attnum > 0 AND NOT a.attisdropped
          AND NOT (rel.relname || '.' || a.attname) = ANY (hidden);
        IF readable IS NOT NULL THEN
            EXECUTE format('GRANT SELECT (%s) ON %I.%I TO %I',
                           readable, app_schema, rel.relname, agent);
        END IF;
        IF rel.relkind IN ('r', 'p') AND NOT rel.relname = ANY (readonly) THEN
            EXECUTE format('GRANT INSERT, UPDATE, DELETE ON %I.%I TO %I',
                           app_schema, rel.relname, agent);
        END IF;
    END LOOP;
    EXECUTE format('GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA %I TO %I',
                   app_schema, agent);

    -- Future tables created by the application role.
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                   'REVOKE ALL ON TABLES FROM %I', owner_role, app_schema, agent);
    IF restrictive THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                       'GRANT SELECT ON TABLES TO %I', owner_role, app_schema, agent);
    ELSE
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                       'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
                       owner_role, app_schema, agent);
    END IF;
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
                   'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I',
                   owner_role, app_schema, agent);

    -- The role must not reach any other database.
    FOR other IN
        SELECT datname FROM pg_database
        WHERE datallowconn AND datname <> database
          AND has_database_privilege(agent, datname, 'CONNECT')
    LOOP
        RAISE WARNING '% can still CONNECT to database %: REVOKE CONNECT ON DATABASE % FROM PUBLIC',
                      agent, other, other;
    END LOOP;
END
$provision$;
