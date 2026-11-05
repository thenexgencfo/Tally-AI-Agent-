-- =====================================================================
-- Row-level security + least-privilege grants.
-- Assumes roles tally_owner (owns objects) and tally_ro (used by the app)
-- already exist -- scripts/setup_db.sh creates them with a password from env.
--
-- Defence in depth: the application ALSO filters by company_id. RLS is the
-- backstop for a bug in that filter, not a substitute for it.
-- =====================================================================
SET search_path = tally, public;

-- app.allowed_companies is set per request by the application, e.g.
--   SELECT set_config('app.allowed_companies', '1,3', true);
-- The 'true' makes it transaction-local so it cannot leak across pooled
-- connections.
CREATE OR REPLACE FUNCTION fn_allowed_companies()
RETURNS integer[]
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT CASE
             WHEN COALESCE(current_setting('app.allowed_companies', true), '') = ''
             THEN ARRAY[]::integer[]
             ELSE string_to_array(current_setting('app.allowed_companies', true), ',')::integer[]
           END;
$$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
      'mst_company','mst_group','mst_ledger','mst_vouchertype','mst_cost_centre',
      'mst_stock_item','trn_voucher','trn_accounting','trn_bill','trn_cost_centre',
      'trn_inventory','sync_state','mst_opening_bill','trn_closingstock'
  ] LOOP
    EXECUTE format('ALTER TABLE tally.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE tally.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_company_isolation ON tally.%I', t);
    EXECUTE format(
      'CREATE POLICY p_company_isolation ON tally.%I FOR SELECT
         USING (company_id = ANY (tally.fn_allowed_companies()))', t);
  END LOOP;
END $$;

-- Least privilege: SELECT + EXECUTE only. No INSERT/UPDATE/DELETE anywhere.
REVOKE ALL ON ALL TABLES IN SCHEMA tally FROM tally_ro;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA tally FROM tally_ro;
GRANT USAGE ON SCHEMA tally TO tally_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA tally TO tally_ro;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA tally TO tally_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tally GRANT SELECT ON TABLES TO tally_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tally GRANT EXECUTE ON FUNCTIONS TO tally_ro;

-- Ingest functions belong to the sync role only.
REVOKE EXECUTE ON FUNCTION tally.fn_ingest(integer, text, text, date, date) FROM tally_ro, PUBLIC;
REVOKE EXECUTE ON FUNCTION tally.fn_mark_sync_failed(integer, text) FROM tally_ro, PUBLIC;

-- Make absolutely sure the app role can never write to the mirror.
REVOKE CREATE ON SCHEMA tally FROM tally_ro;
REVOKE ALL ON SCHEMA public FROM tally_ro;

-- Guard: a view owned by a BYPASSRLS role silently defeats row-level security
-- unless it is declared security_invoker. Fail the deploy if one slips in.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(c.relname, ', ') INTO bad
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'tally' AND c.relkind = 'v'
    AND NOT COALESCE(
      (SELECT option_value::boolean
         FROM pg_options_to_table(c.reloptions)
        WHERE option_name = 'security_invoker'), false);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'views missing security_invoker (RLS bypass risk): %', bad;
  END IF;
END $$;
