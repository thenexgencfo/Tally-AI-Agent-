-- =====================================================================
-- Bridge: tally-database-loader output  ->  company-scoped, RLS-protected mirror.
--
-- The loader writes one company into one schema (its "schema" config option),
-- with no company column. fn_ingest copies that schema into tally.* stamped
-- with company_id, inside ONE transaction: readers see either the previous
-- snapshot or the new one, never a half-loaded company.
--
-- Run as tally_owner (the sync role) after each loader run:
--   SELECT tally.fn_ingest(1, 'loader_c1', 'White Design Consultants',
--                          '2024-04-01', '2025-03-31');
-- Column mapping differences from the loader are noted inline.
-- =====================================================================
SET search_path = tally, public;

CREATE OR REPLACE FUNCTION fn_ingest(p_company integer, p_schema text, p_company_name text,
                                     p_fy_from date DEFAULT NULL, p_fy_to date DEFAULT NULL)
RETURNS bigint
LANGUAGE plpgsql SECURITY INVOKER SET search_path = tally, pg_temp AS $$
DECLARE
    total bigint := 0;
    c     bigint;
    s     text := p_schema;
    t     text;
    bad   text;
    src   text[] := ARRAY['mst_group','mst_ledger','mst_vouchertype','mst_cost_centre',
                          'mst_stock_item','trn_voucher','trn_accounting','trn_bill',
                          'trn_cost_centre','trn_inventory'];
    -- Present in current loader versions; skipped (with a NOTICE) if absent.
    optional text[] := ARRAY['mst_opening_bill_allocation','trn_closingstock_ledger'];
    have  text[];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s) THEN
        RAISE EXCEPTION 'loader schema "%" not found', s;
    END IF;

    -- SECURITY. The loader login owns this schema, so it could plant a view,
    -- a function, a custom type or a child table there whose code would run
    -- with THIS role's privileges the moment we read it. So, before reading:
    --   1. lock every source table (ACCESS SHARE blocks DROP/ALTER, so it
    --      cannot be swapped for a view between the check and the read);
    --   2. accept only plain tables ('r'), with no inheritance children and no
    --      row security, whose columns are all built-in pg_catalog base types.
    -- Reading such a table executes no code owned by the loader.
    FOREACH t IN ARRAY src LOOP
        IF to_regclass(format('%I.%I', s, t)) IS NULL THEN
            RAISE EXCEPTION 'loader table %.% not found', s, t;
        END IF;
    END LOOP;
    have := ARRAY(SELECT o FROM unnest(optional) o WHERE to_regclass(format('%I.%I', s, o)) IS NOT NULL);
    src := src || have;
    FOREACH t IN ARRAY src LOOP
        EXECUTE format('LOCK TABLE %I.%I IN ACCESS SHARE MODE', s, t);
    END LOOP;

    SELECT string_agg(format('%s (relkind %s%s%s)', c2.relname, c2.relkind,
                             CASE WHEN c2.relrowsecurity THEN ', row security' ELSE '' END,
                             CASE WHEN EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhparent = c2.oid)
                                  THEN ', has child tables' ELSE '' END), ', ')
      INTO bad
      FROM pg_class c2 JOIN pg_namespace n ON n.oid = c2.relnamespace
     WHERE n.nspname = s AND c2.relname = ANY (src)
       AND (c2.relkind <> 'r' OR c2.relrowsecurity
            OR EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhparent = c2.oid));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'refusing to ingest from "%": not plain tables: %', s, bad;
    END IF;

    SELECT string_agg(format('%s.%s (%s)', c2.relname, a.attname, format_type(a.atttypid, a.atttypmod)), ', ')
      INTO bad
      FROM pg_attribute a
      JOIN pg_class c2     ON c2.oid = a.attrelid
      JOIN pg_namespace n  ON n.oid = c2.relnamespace
      JOIN pg_type ty      ON ty.oid = a.atttypid
      JOIN pg_namespace tn ON tn.oid = ty.typnamespace
     WHERE n.nspname = s AND c2.relname = ANY (src)
       AND a.attnum > 0 AND NOT a.attisdropped
       AND (tn.nspname <> 'pg_catalog' OR ty.typtype <> 'b');
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'refusing to ingest from "%": non built-in column types: %', s, bad;
    END IF;

    INSERT INTO mst_company (company_id, guid, name, fy_from, fy_to)
    VALUES (p_company, 'cmp-' || p_company, p_company_name, p_fy_from, p_fy_to)
    ON CONFLICT (company_id) DO UPDATE
       SET name    = EXCLUDED.name,
           fy_from = COALESCE(EXCLUDED.fy_from, mst_company.fy_from),
           fy_to   = COALESCE(EXCLUDED.fy_to,   mst_company.fy_to);

    -- Replace this company's snapshot. Children first (FKs).
    DELETE FROM mst_opening_bill WHERE company_id = p_company;
    DELETE FROM trn_closingstock WHERE company_id = p_company;
    DELETE FROM trn_inventory   WHERE company_id = p_company;
    DELETE FROM trn_cost_centre WHERE company_id = p_company;
    DELETE FROM trn_bill        WHERE company_id = p_company;
    DELETE FROM trn_accounting  WHERE company_id = p_company;
    DELETE FROM trn_voucher     WHERE company_id = p_company;
    DELETE FROM mst_stock_item  WHERE company_id = p_company;
    DELETE FROM mst_cost_centre WHERE company_id = p_company;
    DELETE FROM mst_vouchertype WHERE company_id = p_company;
    DELETE FROM mst_ledger      WHERE company_id = p_company;
    DELETE FROM mst_group       WHERE company_id = p_company;

    EXECUTE format($q$
        INSERT INTO mst_group (company_id, guid, name, parent, primary_group, is_revenue,
                               is_deemedpositive, affects_gross_profit, sort_position)
        SELECT %s, guid, COALESCE(name,''), COALESCE(parent,''), COALESCE(primary_group,''),
               COALESCE(is_revenue,0), COALESCE(is_deemedpositive,0),
               COALESCE(affects_gross_profit,0), COALESCE(sort_position,0)
        FROM %I.mst_group $q$, p_company, s);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    -- loader: mobile -> phone, bill_credit_period -> credit_days, no credit_limit
    EXECUTE format($q$
        INSERT INTO mst_ledger (company_id, guid, name, parent, alias, is_revenue, is_deemedpositive,
                                opening_balance, description, mailing_name, mailing_address,
                                mailing_state, mailing_country, mailing_pincode, email, phone,
                                it_pan, gstn, gst_registration_type, bank_account_holder,
                                bank_account_number, bank_ifsc, bank_name, bank_branch, credit_days)
        SELECT %s, guid, COALESCE(name,''), COALESCE(parent,''), COALESCE(alias,''),
               COALESCE(is_revenue,0), COALESCE(is_deemedpositive,0), COALESCE(opening_balance,0),
               COALESCE(description,''), COALESCE(mailing_name,''), COALESCE(mailing_address,''),
               COALESCE(mailing_state,''), COALESCE(mailing_country,''), COALESCE(mailing_pincode,''),
               COALESCE(email,''), COALESCE(mobile,''), COALESCE(it_pan,''), COALESCE(gstn,''),
               COALESCE(gst_registration_type,''), COALESCE(bank_account_holder,''),
               COALESCE(bank_account_number,''), COALESCE(bank_ifsc,''), COALESCE(bank_name,''),
               COALESCE(bank_branch,''), COALESCE(bill_credit_period,0)
        FROM %I.mst_ledger $q$, p_company, s);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    EXECUTE format($q$
        INSERT INTO mst_vouchertype (company_id, guid, name, parent, affects_stock)
        SELECT %s, guid, COALESCE(name,''), COALESCE(parent,''), COALESCE(affects_stock,0)
        FROM %I.mst_vouchertype $q$, p_company, s);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    EXECUTE format($q$
        INSERT INTO mst_cost_centre (company_id, guid, name, parent, category)
        SELECT %s, guid, COALESCE(name,''), COALESCE(parent,''), COALESCE(category,'')
        FROM %I.mst_cost_centre $q$, p_company, s);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    EXECUTE format($q$
        INSERT INTO mst_stock_item (company_id, guid, name, parent, uom, opening_balance,
                                    opening_value, gst_hsn_code)
        SELECT %s, guid, COALESCE(name,''), COALESCE(parent,''), COALESCE(uom,''),
               COALESCE(opening_balance,0), COALESCE(opening_value,0), COALESCE(gst_hsn_code,'')
        FROM %I.mst_stock_item $q$, p_company, s);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    -- The loader's export already filters NOT $IsCancelled and NOT $IsOptional
    -- (tally-export-config.yaml), so these flags are 0 by construction.
    EXECUTE format($q$
        INSERT INTO trn_voucher (company_id, guid, date, voucher_type, voucher_number,
                                 reference_number, reference_date, narration, party_name,
                                 place_of_supply, is_invoice)
        SELECT %s, guid, date, COALESCE(voucher_type,''), COALESCE(voucher_number,''),
               COALESCE(reference_number,''), reference_date, COALESCE(narration,''),
               COALESCE(party_name,''), COALESCE(place_of_supply,''), COALESCE(is_invoice,0)
        FROM %I.trn_voucher WHERE date IS NOT NULL $q$, p_company, s);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    -- loader rows have no line number: synthesise one per voucher.
    EXECUTE format($q$
        INSERT INTO trn_accounting (company_id, guid, line_no, ledger, amount, currency, amount_forex)
        SELECT %s, guid, row_number() OVER (PARTITION BY guid ORDER BY ctid),
               COALESCE(ledger,''), COALESCE(amount,0), COALESCE(NULLIF(currency,''),'INR'),
               COALESCE(amount_forex,0)
        FROM %I.trn_accounting
        WHERE guid IN (SELECT guid FROM tally.trn_voucher WHERE company_id = %s) $q$,
        p_company, s, p_company);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    -- loader: no bill_date -> NULL; reports fall back to the voucher date.
    EXECUTE format($q$
        INSERT INTO trn_bill (company_id, guid, line_no, ledger, name, amount, billtype)
        SELECT %s, guid, row_number() OVER (PARTITION BY guid ORDER BY ctid),
               COALESCE(ledger,''), COALESCE(name,''), COALESCE(amount,0), COALESCE(billtype,'')
        FROM %I.trn_bill
        WHERE guid IN (SELECT guid FROM tally.trn_voucher WHERE company_id = %s) $q$,
        p_company, s, p_company);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    EXECUTE format($q$
        INSERT INTO trn_cost_centre (company_id, guid, line_no, ledger, costcentre, amount)
        SELECT %s, guid, row_number() OVER (PARTITION BY guid ORDER BY ctid),
               COALESCE(ledger,''), COALESCE(costcentre,''), COALESCE(amount,0)
        FROM %I.trn_cost_centre
        WHERE guid IN (SELECT guid FROM tally.trn_voucher WHERE company_id = %s) $q$,
        p_company, s, p_company);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    EXECUTE format($q$
        INSERT INTO trn_inventory (company_id, guid, line_no, item, quantity, rate, amount, godown)
        SELECT %s, guid, row_number() OVER (PARTITION BY guid ORDER BY ctid),
               COALESCE(item,''), COALESCE(quantity,0), COALESCE(rate,0), COALESCE(amount,0),
               COALESCE(godown,'')
        FROM %I.trn_inventory
        WHERE guid IN (SELECT guid FROM tally.trn_voucher WHERE company_id = %s) $q$,
        p_company, s, p_company);
    GET DIAGNOSTICS c = ROW_COUNT; total := total + c;

    IF 'mst_opening_bill_allocation' = ANY (have) THEN
        EXECUTE format($q$
            INSERT INTO mst_opening_bill (company_id, line_no, ledger, name, bill_date, amount, is_advance)
            SELECT %s, row_number() OVER (ORDER BY ctid), COALESCE(ledger,''), COALESCE(name,''),
                   bill_date, COALESCE(opening_balance,0), COALESCE(is_advance,0)
            FROM %I.mst_opening_bill_allocation
            WHERE COALESCE(opening_balance,0) <> 0 $q$, p_company, s);
        GET DIAGNOSTICS c = ROW_COUNT; total := total + c;
    ELSE
        RAISE NOTICE 'loader schema % has no mst_opening_bill_allocation: opening bill dues not loaded', s;
    END IF;

    IF 'trn_closingstock_ledger' = ANY (have) THEN
        EXECUTE format($q$
            INSERT INTO trn_closingstock (company_id, line_no, ledger, stock_date, stock_value)
            SELECT %s, row_number() OVER (ORDER BY ctid), COALESCE(ledger,''), stock_date,
                   COALESCE(stock_value,0)
            FROM %I.trn_closingstock_ledger WHERE stock_date IS NOT NULL $q$, p_company, s);
        GET DIAGNOSTICS c = ROW_COUNT; total := total + c;
    END IF;

    INSERT INTO sync_state (company_id, last_sync_at, last_status, last_error, rows_synced)
    VALUES (p_company, now(), 'ok', '', total)
    ON CONFLICT (company_id) DO UPDATE
       SET last_sync_at = now(), last_status = 'ok', last_error = '', rows_synced = total;
    RETURN total;
END $$;

-- Record a failed run without touching the last good snapshot or its timestamp,
-- so freshness warnings keep telling the truth.
CREATE OR REPLACE FUNCTION fn_mark_sync_failed(p_company integer, p_error text)
RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    INSERT INTO sync_state (company_id, last_sync_at, last_status, last_error)
    VALUES (p_company, 'epoch', 'failed', left(p_error, 2000))
    ON CONFLICT (company_id) DO UPDATE
       SET last_status = 'failed', last_error = left(EXCLUDED.last_error, 2000);
$$;
