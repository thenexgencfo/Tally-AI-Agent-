-- =====================================================================
-- Report templates as SQL functions.
--
-- These ARE the only way the agent can read data. There is no free-form
-- SQL path anywhere in the system: the LLM selects a function name from a
-- fixed registry and fills typed parameters, which are bound (never
-- interpolated). Adding a report means adding a function here plus a
-- registry entry -- a reviewable, auditable change.
--
-- Every function is SECURITY INVOKER with a pinned search_path so it runs
-- under the caller's row-level security and cannot be hijacked by a
-- mutable search_path.
-- =====================================================================
SET search_path = tally, public;

-- Resolve a ledger to its primary (top-level) group.
CREATE OR REPLACE VIEW v_ledger_group WITH (security_invoker = true) AS
SELECT l.company_id,
       l.name                AS ledger,
       l.parent              AS group_name,
       COALESCE(NULLIF(g.primary_group, ''), l.parent) AS primary_group,
       COALESCE(g.is_revenue, l.is_revenue)            AS is_revenue,
       l.opening_balance
FROM mst_ledger l
LEFT JOIN mst_group g
       ON g.company_id = l.company_id AND g.name = l.parent;

-- Net movement per ledger up to (and including) a date.
CREATE OR REPLACE FUNCTION fn_movement(p_company integer, p_from date, p_to date)
RETURNS TABLE (ledger varchar, movement numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT a.ledger, SUM(a.amount)::numeric
    FROM trn_accounting a
    JOIN trn_voucher v
      ON v.company_id = a.company_id AND v.guid = a.guid
    WHERE a.company_id = p_company
      AND v.is_cancelled = 0 AND v.is_optional = 0
      AND (p_from IS NULL OR v.date >= p_from)
      AND v.date <= p_to
    GROUP BY a.ledger;
$$;

-- ------------------------------------------------------------ 1. trial balance
CREATE OR REPLACE FUNCTION fn_trial_balance(p_company integer, p_to date)
RETURNS TABLE (ledger varchar, group_name varchar, debit numeric, credit numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.ledger::varchar,
           lg.group_name::varchar,
           GREATEST(-(lg.opening_balance + COALESCE(m.movement, 0)), 0)::numeric AS debit,
           GREATEST( (lg.opening_balance + COALESCE(m.movement, 0)), 0)::numeric AS credit
    FROM v_ledger_group lg
    LEFT JOIN fn_movement(p_company, NULL, p_to) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND ABS(lg.opening_balance + COALESCE(m.movement, 0)) > 0.005
    ORDER BY lg.group_name, lg.ledger;
$$;

-- ----------------------------------------------------------- 2. ledger balance
CREATE OR REPLACE FUNCTION fn_ledger_balance(p_company integer, p_ledger text, p_to date)
RETURNS TABLE (ledger varchar, group_name varchar, closing numeric, dr_cr text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.ledger::varchar,
           lg.group_name::varchar,
           ABS(lg.opening_balance + COALESCE(m.movement, 0))::numeric,
           CASE WHEN (lg.opening_balance + COALESCE(m.movement, 0)) < 0
                THEN 'Dr' ELSE 'Cr' END::text
    FROM v_ledger_group lg
    LEFT JOIN fn_movement(p_company, NULL, p_to) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND lower(lg.ledger) = lower(p_ledger)
    ORDER BY lg.ledger;
$$;

-- --------------------------------------------------------- 3. ledger statement
CREATE OR REPLACE FUNCTION fn_ledger_statement(p_company integer, p_ledger text,
                                               p_from date, p_to date, p_limit integer)
RETURNS TABLE (txn_date date, voucher_type varchar, voucher_number varchar,
               narration text, debit numeric, credit numeric, running_balance numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH opening AS (
        SELECT lg.opening_balance + COALESCE((
                   SELECT SUM(a2.amount)
                   FROM trn_accounting a2
                   JOIN trn_voucher v2 ON v2.company_id = a2.company_id AND v2.guid = a2.guid
                   WHERE a2.company_id = p_company AND a2.ledger = lg.ledger
                     AND v2.date < p_from AND v2.is_cancelled = 0 AND v2.is_optional = 0
               ), 0) AS bal
        FROM v_ledger_group lg
        WHERE lg.company_id = p_company AND lower(lg.ledger) = lower(p_ledger)
    ), lines AS (
        SELECT v.date, v.voucher_type, v.voucher_number, v.narration, a.amount
        FROM trn_accounting a
        JOIN trn_voucher v ON v.company_id = a.company_id AND v.guid = a.guid
        WHERE a.company_id = p_company AND lower(a.ledger) = lower(p_ledger)
          AND v.date BETWEEN p_from AND p_to
          AND v.is_cancelled = 0 AND v.is_optional = 0
        ORDER BY v.date, v.voucher_number
        LIMIT LEAST(COALESCE(p_limit, 200), 1000)
    )
    SELECT l.date,
           l.voucher_type::varchar,
           l.voucher_number::varchar,
           l.narration,
           GREATEST(-l.amount, 0)::numeric,
           GREATEST( l.amount, 0)::numeric,
           ABS((SELECT bal FROM opening) + SUM(l.amount) OVER (ORDER BY l.date, l.voucher_number
                                                               ROWS UNBOUNDED PRECEDING))::numeric
    FROM lines l;
$$;

-- ------------------------------------------------- 4. bills outstanding + ageing
CREATE OR REPLACE FUNCTION fn_bills_outstanding(p_company integer, p_group text,
                                                p_asof date, p_limit integer)
RETURNS TABLE (party varchar, bill_ref varchar, bill_date date, age_days integer,
               bucket text, outstanding numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH bills AS (
        SELECT b.ledger,
               b.name,
               SUM(b.amount) AS bal,
               MIN(COALESCE(b.bill_date, v.date)) FILTER (WHERE b.billtype = 'New Ref') AS ref_date,
               MIN(COALESCE(b.bill_date, v.date)) AS any_date
        FROM trn_bill b
        JOIN trn_voucher v ON v.company_id = b.company_id AND v.guid = b.guid
        JOIN v_ledger_group lg ON lg.company_id = b.company_id AND lg.ledger = b.ledger
        WHERE b.company_id = p_company
          AND v.date <= p_asof
          AND v.is_cancelled = 0 AND v.is_optional = 0
          AND lower(lg.primary_group) = lower(p_group)
        GROUP BY b.ledger, b.name
    )
    SELECT ledger::varchar,
           name::varchar,
           COALESCE(ref_date, any_date) AS bill_date,
           (p_asof - COALESCE(ref_date, any_date))::integer AS age_days,
           CASE
             WHEN (p_asof - COALESCE(ref_date, any_date)) <=  30 THEN '0-30'
             WHEN (p_asof - COALESCE(ref_date, any_date)) <=  60 THEN '31-60'
             WHEN (p_asof - COALESCE(ref_date, any_date)) <=  90 THEN '61-90'
             WHEN (p_asof - COALESCE(ref_date, any_date)) <= 180 THEN '91-180'
             ELSE '180+'
           END::text,
           ABS(bal)::numeric
    FROM bills
    WHERE ABS(bal) > 0.005
    ORDER BY 4 DESC
    LIMIT LEAST(COALESCE(p_limit, 200), 1000);
$$;

-- --------------------------------------------------------- 5. ageing summary
CREATE OR REPLACE FUNCTION fn_ageing_summary(p_company integer, p_group text, p_asof date)
RETURNS TABLE (bucket text, bill_count bigint, outstanding numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT b.bucket, COUNT(*)::bigint, SUM(b.outstanding)::numeric
    FROM fn_bills_outstanding(p_company, p_group, p_asof, 1000) b
    GROUP BY b.bucket
    ORDER BY CASE b.bucket WHEN '0-30' THEN 1 WHEN '31-60' THEN 2
                           WHEN '61-90' THEN 3 WHEN '91-180' THEN 4 ELSE 5 END;
$$;

-- ------------------------------------------------------------- 6. profit & loss
CREATE OR REPLACE FUNCTION fn_profit_loss(p_company integer, p_from date, p_to date)
RETURNS TABLE (group_name varchar, ledger varchar, amount numeric, section text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.group_name::varchar,
           lg.ledger::varchar,
           ABS(COALESCE(m.movement, 0))::numeric,
           CASE WHEN COALESCE(m.movement, 0) > 0 THEN 'Income' ELSE 'Expense' END::text
    FROM v_ledger_group lg
    JOIN fn_movement(p_company, p_from, p_to) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND lg.is_revenue = 1
      AND ABS(COALESCE(m.movement, 0)) > 0.005
    ORDER BY 4, 3 DESC;
$$;

CREATE OR REPLACE FUNCTION fn_profit_loss_summary(p_company integer, p_from date, p_to date)
RETURNS TABLE (total_income numeric, total_expense numeric, net_profit numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT COALESCE(SUM(amount) FILTER (WHERE section = 'Income'), 0)::numeric,
           COALESCE(SUM(amount) FILTER (WHERE section = 'Expense'), 0)::numeric,
           (COALESCE(SUM(amount) FILTER (WHERE section = 'Income'), 0)
          - COALESCE(SUM(amount) FILTER (WHERE section = 'Expense'), 0))::numeric
    FROM fn_profit_loss(p_company, p_from, p_to);
$$;

-- ------------------------------------------------------------ 7. balance sheet
CREATE OR REPLACE FUNCTION fn_balance_sheet(p_company integer, p_asof date)
RETURNS TABLE (primary_group varchar, group_name varchar, ledger varchar,
               amount numeric, side text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.primary_group::varchar,
           lg.group_name::varchar,
           lg.ledger::varchar,
           ABS(lg.opening_balance + COALESCE(m.movement, 0))::numeric,
           CASE WHEN (lg.opening_balance + COALESCE(m.movement, 0)) < 0
                THEN 'Assets' ELSE 'Liabilities' END::text
    FROM v_ledger_group lg
    LEFT JOIN fn_movement(p_company, NULL, p_asof) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND lg.is_revenue = 0
      AND ABS(lg.opening_balance + COALESCE(m.movement, 0)) > 0.005
    ORDER BY 5, 1, 3;
$$;

-- ---------------------------------------------------------------- 8. day book
CREATE OR REPLACE FUNCTION fn_day_book(p_company integer, p_from date, p_to date,
                                       p_voucher_type text, p_limit integer)
RETURNS TABLE (txn_date date, voucher_type varchar, voucher_number varchar,
               party varchar, narration text, amount numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT v.date, v.voucher_type::varchar, v.voucher_number::varchar,
           v.party_name::varchar, v.narration,
           COALESCE((SELECT SUM(GREATEST(a.amount, 0))
                     FROM trn_accounting a
                     WHERE a.company_id = v.company_id AND a.guid = v.guid), 0)::numeric
    FROM trn_voucher v
    WHERE v.company_id = p_company
      AND v.date BETWEEN p_from AND p_to
      AND v.is_cancelled = 0 AND v.is_optional = 0
      AND (p_voucher_type IS NULL OR p_voucher_type = ''
           OR lower(v.voucher_type) = lower(p_voucher_type))
    ORDER BY v.date DESC, v.voucher_number DESC
    LIMIT LEAST(COALESCE(p_limit, 100), 1000);
$$;

-- ------------------------------------------------------------ 9. top parties
CREATE OR REPLACE FUNCTION fn_top_parties(p_company integer, p_from date, p_to date,
                                          p_voucher_type text, p_limit integer)
RETURNS TABLE (party varchar, voucher_count bigint, total_amount numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT v.party_name::varchar, COUNT(*)::bigint,
           SUM(COALESCE((SELECT SUM(GREATEST(a.amount, 0))
                         FROM trn_accounting a
                         WHERE a.company_id = v.company_id AND a.guid = v.guid), 0))::numeric
    FROM trn_voucher v
    WHERE v.company_id = p_company
      AND v.date BETWEEN p_from AND p_to
      AND v.is_cancelled = 0 AND v.is_optional = 0
      AND COALESCE(v.party_name, '') <> ''
      AND (p_voucher_type IS NULL OR p_voucher_type = ''
           OR lower(v.voucher_type) = lower(p_voucher_type))
    GROUP BY v.party_name
    ORDER BY 3 DESC
    LIMIT LEAST(COALESCE(p_limit, 10), 100);
$$;

-- -------------------------------------------------------- 10. cash & bank
CREATE OR REPLACE FUNCTION fn_cash_bank_balance(p_company integer, p_asof date)
RETURNS TABLE (ledger varchar, group_name varchar, balance numeric, dr_cr text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.ledger::varchar, lg.group_name::varchar,
           ABS(lg.opening_balance + COALESCE(m.movement, 0))::numeric,
           CASE WHEN (lg.opening_balance + COALESCE(m.movement, 0)) < 0
                THEN 'Dr' ELSE 'Cr' END::text
    FROM v_ledger_group lg
    LEFT JOIN fn_movement(p_company, NULL, p_asof) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND lower(lg.primary_group) IN ('cash-in-hand', 'bank accounts', 'bank od a/c')
    ORDER BY lg.ledger;
$$;

-- ------------------------------------------------------------ 11. GST summary
CREATE OR REPLACE FUNCTION fn_gst_summary(p_company integer, p_from date, p_to date)
RETURNS TABLE (ledger varchar, group_name varchar, amount numeric, dr_cr text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.ledger::varchar, lg.group_name::varchar,
           ABS(COALESCE(m.movement, 0))::numeric,
           CASE WHEN COALESCE(m.movement, 0) < 0 THEN 'Dr' ELSE 'Cr' END::text
    FROM v_ledger_group lg
    JOIN fn_movement(p_company, p_from, p_to) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND lower(lg.primary_group) = 'duties & taxes'
      AND ABS(COALESCE(m.movement, 0)) > 0.005
    ORDER BY ABS(COALESCE(m.movement, 0)) DESC;
$$;

-- ------------------------------------------------------ 12. voucher search
CREATE OR REPLACE FUNCTION fn_voucher_search(p_company integer, p_party text,
                                             p_from date, p_to date,
                                             p_min_amount numeric, p_limit integer)
RETURNS TABLE (txn_date date, voucher_type varchar, voucher_number varchar,
               party varchar, narration text, amount numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT v.date, v.voucher_type::varchar, v.voucher_number::varchar,
           v.party_name::varchar, v.narration, t.amt::numeric
    FROM trn_voucher v
    CROSS JOIN LATERAL (
        SELECT COALESCE(SUM(GREATEST(a.amount, 0)), 0) AS amt
        FROM trn_accounting a
        WHERE a.company_id = v.company_id AND a.guid = v.guid
    ) t
    WHERE v.company_id = p_company
      AND v.date BETWEEN p_from AND p_to
      AND v.is_cancelled = 0 AND v.is_optional = 0
      AND (p_party IS NULL OR p_party = '' OR v.party_name ILIKE '%' || p_party || '%')
      AND (p_min_amount IS NULL OR t.amt >= p_min_amount)
    ORDER BY v.date DESC
    LIMIT LEAST(COALESCE(p_limit, 100), 1000);
$$;

-- ------------------------------------------------- 13. party master lookup
CREATE OR REPLACE FUNCTION fn_party_details(p_company integer, p_party text)
RETURNS TABLE (ledger varchar, group_name varchar, gstn varchar, it_pan varchar,
               email varchar, phone varchar, mailing_state varchar,
               credit_limit numeric, credit_days integer)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT l.name::varchar, l.parent::varchar, l.gstn::varchar, l.it_pan::varchar,
           l.email::varchar, l.phone::varchar, l.mailing_state::varchar,
           l.credit_limit::numeric, l.credit_days::integer
    FROM mst_ledger l
    WHERE l.company_id = p_company
      AND l.name ILIKE '%' || p_party || '%'
    ORDER BY l.name
    LIMIT 25;
$$;
