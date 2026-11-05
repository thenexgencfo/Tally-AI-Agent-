#!/usr/bin/env bash
# Create roles + schema for the Tally mirror. Idempotent.
#
#   PGSUPER   : superuser psql connection (default: local 'postgres' peer auth)
#   DB_NAME   : database to create          (default: tally)
#   RO_PASS   : password for tally_ro       (REQUIRED - the app role)
#   OWNER_PASS: password for tally_owner    (REQUIRED - the sync role)
#   SEED      : 1 to load demo data         (default: 0)  NEVER set in production
set -euo pipefail

DB_NAME="${DB_NAME:-tally}"
RO_PASS="${RO_PASS:?set RO_PASS}"
OWNER_PASS="${OWNER_PASS:?set OWNER_PASS}"
SEED="${SEED:-0}"
PSQL_SUPER=(${PGSUPER:-sudo -u postgres psql} -v ON_ERROR_STOP=1 -q)

# Passwords are embedded in SQL below: allow only a safe alphabet, and require
# length unless explicitly running a throwaway dev database.
for v in RO_PASS OWNER_PASS; do
  val="${!v}"
  if [[ ! "$val" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "ERROR: $v may contain only A-Z a-z 0-9 _ -" >&2; exit 2
  fi
  if [[ "${ALLOW_WEAK_PASSWORDS:-0}" != "1" && ${#val} -lt 24 ]]; then
    echo "ERROR: $v must be at least 24 characters (openssl rand -hex 16)" >&2; exit 2
  fi
done

here="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> roles"
"${PSQL_SUPER[@]}" -d postgres <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tally_owner') THEN
    CREATE ROLE tally_owner LOGIN BYPASSRLS PASSWORD '${OWNER_PASS}';
  ELSE
    ALTER ROLE tally_owner LOGIN BYPASSRLS PASSWORD '${OWNER_PASS}';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tally_ro') THEN
    CREATE ROLE tally_ro LOGIN NOBYPASSRLS PASSWORD '${RO_PASS}';
  ELSE
    ALTER ROLE tally_ro LOGIN NOBYPASSRLS PASSWORD '${RO_PASS}';
  END IF;
END \$\$;
SQL

echo "==> database ${DB_NAME}"
"${PSQL_SUPER[@]}" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || "${PSQL_SUPER[@]}" -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER tally_owner"

"${PSQL_SUPER[@]}" -d "${DB_NAME}" -c "GRANT CONNECT ON DATABASE ${DB_NAME} TO tally_ro"

# Schemas must be owned by tally_owner: referential-integrity triggers execute
# as the owner of the referencing table and need USAGE on the schema.
"${PSQL_SUPER[@]}" -d "${DB_NAME}" <<SQL
CREATE SCHEMA IF NOT EXISTS tally AUTHORIZATION tally_owner;
CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION tally_owner;
ALTER SCHEMA tally OWNER TO tally_owner;
ALTER SCHEMA audit OWNER TO tally_owner;
SQL

echo "==> schema objects (as tally_owner)"
for f in 01_schema.sql 02_sync_meta.sql 03_reports.sql 06_ingest.sql; do
  echo "    - $f"
  "${PSQL_SUPER[@]}" -d "${DB_NAME}" -f "${here}/sql/$f" >/dev/null
done

if [ "${SEED}" = "1" ]; then
  echo "==> demo seed (NOT for production)"
  "${PSQL_SUPER[@]}" -d "${DB_NAME}" -f "${here}/sql/99_seed_demo.sql" >/dev/null
fi

echo "==> audit + row-level security (applied last)"
"${PSQL_SUPER[@]}" -d "${DB_NAME}" -f "${here}/sql/05_audit.sql" >/dev/null
"${PSQL_SUPER[@]}" -d "${DB_NAME}" -f "${here}/sql/04_security.sql" >/dev/null

# Objects created by the superuser must end up owned by tally_owner.
"${PSQL_SUPER[@]}" -d "${DB_NAME}" <<'SQL' >/dev/null
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname, n.nspname FROM pg_class c
           JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname IN ('tally','audit') AND c.relkind IN ('r','v','S')
             -- sequences owned by a table column follow their table automatically
             AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid
                             AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a','i'))
  LOOP EXECUTE format('ALTER TABLE %I.%I OWNER TO tally_owner', r.nspname, r.relname); END LOOP;
  FOR r IN SELECT p.proname, n.nspname,
                  pg_get_function_identity_arguments(p.oid) AS args
           FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname IN ('tally','audit')
  LOOP EXECUTE format('ALTER FUNCTION %I.%I(%s) OWNER TO tally_owner',
                      r.nspname, r.proname, r.args); END LOOP;
END $$;
SQL

echo "==> done. app DSN: postgresql://tally_ro:***@127.0.0.1:5432/${DB_NAME}"
