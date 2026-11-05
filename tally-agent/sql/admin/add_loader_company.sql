-- =====================================================================
-- Provision one Tally company for tally-database-loader.
--
-- The loader's "schema" setting is a DATABASE name, and it creates tables
-- without a schema prefix. So each company gets its own login role whose
-- search_path is a schema of the same name: the loader for company 1 logs in
-- as loader_c1 and its tables land in schema loader_c1 of the tally database.
-- Separate credentials per company; the app role (tally_ro) cannot see any
-- loader schema; only tally_owner (the ingest role) can read them.
--
-- Usage (superuser):
--   psql -U postgres -d tally -v role=loader_c1 -v pass='...' -f add_loader_company.sql
-- =====================================================================
\set ON_ERROR_STOP on
-- Never write the CREATE/ALTER ROLE ... PASSWORD statements below to the server log.
SET log_statement = 'none';

SELECT (:'role' ~ '^loader_c[0-9]{1,6}$') AS role_ok \gset
\if :role_ok
\else
  DO $$ BEGIN RAISE EXCEPTION 'role must look like loader_c<company_id>'; END $$;
\endif

SELECT format('CREATE ROLE %I LOGIN NOBYPASSRLS PASSWORD %L', :'role', :'pass')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I LOGIN NOBYPASSRLS PASSWORD %L', :'role', :'pass') \gexec
SELECT format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I', :'role', :'role') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET search_path = %I',
              :'role', current_database(), :'role') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'role') \gexec
-- Loader may not create anything in public (PG15+ default, made explicit).
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', :'role') \gexec
-- Ingest role reads the loader's tables, now and as the loader recreates them.
SELECT format('GRANT USAGE ON SCHEMA %I TO tally_owner', :'role') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO tally_owner', :'role') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO tally_owner',
              :'role', :'role') \gexec
-- Defence in depth: loader logins cannot create functions at all.
-- (tally.fn_ingest also refuses anything but plain tables of built-in types.)
REVOKE USAGE ON LANGUAGE plpgsql FROM PUBLIC;
REVOKE USAGE ON LANGUAGE sql FROM PUBLIC;
GRANT USAGE ON LANGUAGE plpgsql TO tally_owner;
GRANT USAGE ON LANGUAGE sql TO tally_owner;
\echo 'provisioned' :role
