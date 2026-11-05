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
--
-- Totals are never computed from a truncated row set: functions that list
-- rows take p_limit, where NULL means "all rows", and the application asks
-- for all rows and caps only what it DISPLAYS.
-- =====================================================================
SET search_path = tally, public;

-- Signatures/return types changed in this revision.
DROP FUNCTION IF EXISTS fn_bills_outstanding(integer, text, date, integer);
DROP FUNCTION IF EXISTS fn_ageing_summary(integer, text, date);
DROP VIEW IF EXISTS v_ledger_group CASCADE;
DROP VIEW IF EXISTS v_group_path CASCADE;

-- ------------------------------------------------------- group hierarchy
-- The loader's primary_group is Tally's top-level group ("Current Assets"),
-- not the group reports care about ("Sundry Debtors" sits UNDER Current
-- Assets). So walk the parent chain and keep every ancestor's name.
CREATE VIEW v_group_path WITH (security_invoker = true) AS
WITH RECURSIVE walk AS (
    SELECT g.company_id, g.name AS grp, g.parent,
           ARRAY[lower(g.name)]::text[] AS path, 1 AS depth
    FROM mst_group g
    UNION ALL
    SELECT w.company_id, w.grp, p.parent, w.path || lower(p.name), w.depth + 1
    FROM walk w
    JOIN mst_group p ON p.company_id = w.company_id AND p.name = w.parent
    WHERE w.parent <> '' AND w.depth < 25 AND NOT (lower(p.name) = ANY (w.path))
)
SELECT DISTINCT ON (company_id, grp) company_id, grp, path
FROM walk
ORDER BY company_id, grp, depth DESC;

CREATE VIEW v_ledger_group WITH (security_invoker = true) AS
SELECT l.company_id,
       l.name                AS ledger,
       l.parent              AS group_name,
       COALESCE(NULLIF(g.primary_group, ''), l.parent) AS primary_group,
       COALESCE(g.is_revenue, l.is_revenue)            AS is_revenue,
       l.opening_balance,
       COALESCE(gp.path, CASE WHEN l.parent = '' THEN ARRAY[]::text[]
                              ELSE ARRAY[lower(l.parent)] END) AS group_path
FROM mst_ledger l
LEFT JOIN mst_group g
       ON g.company_id = l.company_id AND g.name = l.parent
LEFT JOIN v_group_path gp
       ON gp.company_id = l.company_id AND gp.grp = l.parent;

-- Net movement per ledger in a date range (p_from NULL = from the beginning).
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

-- First day of the financial year containing p_date (company FY start, else 1 April).
CREATE OR REPLACE FUNCTION fn_fy_start(p_company integer, p_date date)
RETURNS date
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH m AS (
        SELECT COALESCE(EXTRACT(MONTH FROM (SELECT fy_from FROM mst_company
                                            WHERE company_id = p_company))::int, 4) AS mon
    )
    SELECT make_date(CASE WHEN EXTRACT(MONTH FROM p_date)::int >= m.mon
                          THEN EXTRACT(YEAR FROM p_date)::int
                          ELSE EXTRACT(YEAR FROM p_date)::int - 1 END, m.mon, 1)
    FROM m;
$$;

-- Value of one Stock-in-Hand ledger on a date: the latest closing-stock value
-- entered on or before that date, else the ledger's opening (books-start) value.
-- Returned as a positive amount.
CREATE OR REPLACE FUNCTION fn_stock_ledger_value(p_company integer, p_ledger text, p_date date)
RETURNS numeric
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT COALESCE(
        (SELECT cs.stock_value FROM trn_closingstock cs
          WHERE cs.company_id = p_company AND cs.ledger = p_ledger AND cs.stock_date <= p_date
          ORDER BY cs.stock_date DESC LIMIT 1),
        (SELECT -l.opening_balance FROM mst_ledger l
          WHERE l.company_id = p_company AND l.name = p_ledger),
        0)::numeric;
$$;

CREATE OR REPLACE FUNCTION fn_stock_value(p_company integer, p_date date)
RETURNS numeric
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT COALESCE(SUM(fn_stock_ledger_value(p_company, lg.ledger, p_date)), 0)::numeric
    FROM v_ledger_group lg
    WHERE lg.company_id = p_company AND 'stock-in-hand' = ANY (lg.group_path);
$$;

-- Balance of every ledger as on p_to, Tally sign convention (debit negative).
--   p_fy_bounded = true : P&L ledgers show only the current financial year, and
--                         earlier profit is carried into "Profit & Loss A/c"
--                         (trial balance view).
--   p_fy_bounded = false: P&L ledgers are collapsed entirely into
--                         "Profit & Loss A/c" (balance sheet view).
-- Stock-in-Hand ledgers are valued at the closing stock on the relevant date.
-- Either way the result sums to zero for balanced books.
CREATE OR REPLACE FUNCTION fn_balances(p_company integer, p_to date, p_fy_bounded boolean)
RETURNS TABLE (ledger varchar, group_name varchar, primary_group varchar, is_revenue smallint,
               balance numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH fy AS (SELECT fn_fy_start(p_company, p_to) AS d),
    lg AS (SELECT * FROM v_ledger_group WHERE company_id = p_company),
    allmv AS (SELECT * FROM fn_movement(p_company, NULL, p_to)),
    fymv  AS (SELECT * FROM fn_movement(p_company, (SELECT d FROM fy), p_to)),
    -- the date whose stock value stands in the balance sheet / trial balance
    stock_on AS (SELECT CASE WHEN p_fy_bounded THEN (SELECT d FROM fy) - 1 ELSE p_to END AS d),
    stock_open AS (
        SELECT COALESCE(SUM(-lg.opening_balance), 0) AS v FROM lg
        WHERE 'stock-in-hand' = ANY (lg.group_path)),
    -- profit carried into Profit & Loss A/c: revenue movement outside the shown
    -- period, plus the change in stock value since the books began.
    carry AS (
        SELECT COALESCE((
                   SELECT SUM(m.movement)
                   FROM fn_movement(p_company, NULL,
                                    CASE WHEN p_fy_bounded THEN (SELECT d FROM fy) - 1 ELSE p_to END) m
                   JOIN lg ON lg.ledger = m.ledger
                   WHERE lg.is_revenue = 1), 0)
             + fn_stock_value(p_company, (SELECT d FROM stock_on))
             - (SELECT v FROM stock_open) AS amt),
    base AS (
        SELECT lg.ledger, lg.group_name, lg.primary_group, lg.is_revenue::smallint AS is_revenue,
               CASE
                 WHEN lg.is_revenue = 1 THEN
                      CASE WHEN p_fy_bounded THEN COALESCE(f.movement, 0) ELSE 0 END
                 WHEN 'stock-in-hand' = ANY (lg.group_path) THEN
                      -fn_stock_ledger_value(p_company, lg.ledger, (SELECT d FROM stock_on))
                      + COALESCE(a.movement, 0)
                 ELSE lg.opening_balance + COALESCE(a.movement, 0)
               END AS balance
        FROM lg
        LEFT JOIN allmv a ON a.ledger = lg.ledger
        LEFT JOIN fymv  f ON f.ledger = lg.ledger
        UNION ALL
        -- Ledgers used in vouchers but missing from the ledger list (deleted or
        -- renamed in Tally after posting). Shown, not dropped, so the totals
        -- still add up and the reason for any difference is visible.
        SELECT a.ledger, '(not in ledger masters)'::varchar, '(unknown)'::varchar, 0::smallint,
               a.movement
        FROM allmv a
        WHERE NOT EXISTS (SELECT 1 FROM lg WHERE lg.ledger = a.ledger)
    ),
    merged AS (
        SELECT b.ledger, b.group_name, b.primary_group, b.is_revenue,
               b.balance + CASE WHEN lower(b.ledger) = 'profit & loss a/c'
                                THEN (SELECT amt FROM carry) ELSE 0 END AS balance
        FROM base b
        UNION ALL
        SELECT 'Profit & Loss A/c'::varchar, ''::varchar, 'Profit & Loss A/c'::varchar, 0::smallint,
               (SELECT amt FROM carry)
        WHERE NOT EXISTS (SELECT 1 FROM base WHERE lower(base.ledger) = 'profit & loss a/c')
    )
    SELECT ledger::varchar, group_name::varchar, primary_group::varchar, is_revenue, balance::numeric
    FROM merged
    WHERE ABS(balance) > 0.005;
$$;

-- ------------------------------------------------------------ 1. trial balance
CREATE OR REPLACE FUNCTION fn_trial_balance(p_company integer, p_to date)
RETURNS TABLE (ledger varchar, group_name varchar, debit numeric, credit numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT b.ledger, b.group_name,
           GREATEST(-b.balance, 0)::numeric AS debit,
           GREATEST( b.balance, 0)::numeric AS credit
    FROM fn_balances(p_company, p_to, true) b
    ORDER BY b.group_name, b.ledger;
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
      AND lower(btrim(lg.ledger)) = lower(btrim(p_ledger))
    ORDER BY lg.ledger;
$$;

-- --------------------------------------------------------- 3. ledger statement
-- p_limit NULL = every transaction (the running balance needs all of them).
CREATE OR REPLACE FUNCTION fn_ledger_statement(p_company integer, p_ledger text,
                                               p_from date, p_to date, p_limit integer)
RETURNS TABLE (txn_date date, voucher_type varchar, voucher_number varchar,
               narration text, debit numeric, credit numeric, running_balance numeric,
               dr_cr text)
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
        WHERE lg.company_id = p_company AND lower(btrim(lg.ledger)) = lower(btrim(p_ledger))
    ), lines AS (
        SELECT v.date, v.voucher_type, v.voucher_number, v.narration, a.amount,
               (SELECT bal FROM opening)
                 + SUM(a.amount) OVER (ORDER BY v.date, v.voucher_number, a.guid, a.line_no
                                       ROWS UNBOUNDED PRECEDING) AS bal
        FROM trn_accounting a
        JOIN trn_voucher v ON v.company_id = a.company_id AND v.guid = a.guid
        WHERE a.company_id = p_company AND lower(btrim(a.ledger)) = lower(btrim(p_ledger))
          AND v.date BETWEEN p_from AND p_to
          AND v.is_cancelled = 0 AND v.is_optional = 0
    )
    SELECT l.date, l.voucher_type::varchar, l.voucher_number::varchar, l.narration,
           GREATEST(-l.amount, 0)::numeric, GREATEST(l.amount, 0)::numeric,
           ABS(l.bal)::numeric, CASE WHEN l.bal < 0 THEN 'Dr' ELSE 'Cr' END::text
    FROM lines l
    ORDER BY l.date, l.voucher_number
    LIMIT p_limit;
$$;

-- ------------------------------------------------- 4. bills outstanding + ageing
-- outstanding is SIGNED from the firm's point of view: positive = due to be
-- received (debtors) or paid (creditors); negative = an advance / on-account
-- amount that reduces what is due. Opening bill-wise dues are included.
CREATE OR REPLACE FUNCTION fn_bills_outstanding(p_company integer, p_group text,
                                                p_asof date, p_limit integer)
RETURNS TABLE (party varchar, bill_ref varchar, bill_date date, age_days integer,
               bucket text, outstanding numeric, kind text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH lines AS (
        SELECT b.ledger, b.name, b.amount, b.billtype, COALESCE(b.bill_date, v.date) AS d
        FROM trn_bill b
        JOIN trn_voucher v ON v.company_id = b.company_id AND v.guid = b.guid
        WHERE b.company_id = p_company
          AND v.date <= p_asof
          AND v.is_cancelled = 0 AND v.is_optional = 0
        UNION ALL
        SELECT o.ledger, o.name, o.amount,
               CASE WHEN o.is_advance = 1 THEN 'Advance' ELSE 'New Ref' END, o.bill_date
        FROM mst_opening_bill o
        WHERE o.company_id = p_company
    ), bills AS (
        SELECT l.ledger, l.name,
               SUM(l.amount) AS bal,
               MIN(l.d) FILTER (WHERE l.billtype IN ('New Ref', 'Advance')) AS ref_date,
               MIN(l.d) AS any_date
        FROM lines l
        JOIN v_ledger_group lg ON lg.company_id = p_company AND lg.ledger = l.ledger
        WHERE lower(p_group) = ANY (lg.group_path)
        GROUP BY l.ledger, l.name
    ), due AS (
        SELECT ledger, name, COALESCE(ref_date, any_date) AS bdate,
               CASE WHEN lower(p_group) = 'sundry creditors' THEN bal ELSE -bal END AS amt
        FROM bills
        WHERE ABS(bal) > 0.005
    )
    SELECT ledger::varchar, name::varchar, bdate,
           (p_asof - bdate)::integer,
           CASE
             WHEN amt < 0 THEN 'Advance'
             WHEN (p_asof - bdate) <=  30 THEN '0-30'
             WHEN (p_asof - bdate) <=  60 THEN '31-60'
             WHEN (p_asof - bdate) <=  90 THEN '61-90'
             WHEN (p_asof - bdate) <= 180 THEN '91-180'
             ELSE '180+'
           END::text,
           amt::numeric,
           CASE WHEN amt < 0 THEN 'Advance' ELSE 'Bill' END::text
    FROM due
    ORDER BY (amt < 0), 4 DESC NULLS LAST, 1, 2
    LIMIT p_limit;
$$;

-- --------------------------------------------------------- 5. ageing summary
-- Aggregates EVERY bill (no limit). Advances are their own bucket, negative.
CREATE OR REPLACE FUNCTION fn_ageing_summary(p_company integer, p_group text, p_asof date)
RETURNS TABLE (bucket text, bill_count bigint, outstanding numeric)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT b.bucket, COUNT(*)::bigint, SUM(b.outstanding)::numeric
    FROM fn_bills_outstanding(p_company, p_group, p_asof, NULL) b
    GROUP BY b.bucket
    ORDER BY CASE b.bucket WHEN '0-30' THEN 1 WHEN '31-60' THEN 2 WHEN '61-90' THEN 3
                           WHEN '91-180' THEN 4 WHEN '180+' THEN 5 ELSE 6 END;
$$;

-- ------------------------------------------------------------- 6. profit & loss
-- Includes opening and closing stock when the company keeps Stock-in-Hand
-- ledgers with closing values.
CREATE OR REPLACE FUNCTION fn_profit_loss(p_company integer, p_from date, p_to date)
RETURNS TABLE (group_name varchar, ledger varchar, amount numeric, section text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH has_stock AS (
        SELECT EXISTS (SELECT 1 FROM v_ledger_group lg
                       WHERE lg.company_id = p_company AND 'stock-in-hand' = ANY (lg.group_path)) AS yes
    ), rows AS (
        SELECT lg.group_name::varchar AS group_name, lg.ledger::varchar AS ledger,
               ABS(m.movement)::numeric AS amount,
               CASE WHEN m.movement > 0 THEN 'Income' ELSE 'Expense' END::text AS section
        FROM v_ledger_group lg
        JOIN fn_movement(p_company, p_from, p_to) m ON m.ledger = lg.ledger
        WHERE lg.company_id = p_company AND lg.is_revenue = 1 AND ABS(m.movement) > 0.005
        UNION ALL
        SELECT 'Stock-in-Hand'::varchar, 'Opening Stock'::varchar,
               fn_stock_value(p_company, p_from - 1), 'Expense'::text
        WHERE (SELECT yes FROM has_stock)
        UNION ALL
        SELECT 'Stock-in-Hand'::varchar, 'Closing Stock'::varchar,
               fn_stock_value(p_company, p_to), 'Income'::text
        WHERE (SELECT yes FROM has_stock)
    )
    SELECT * FROM rows WHERE amount > 0.005
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
-- All profit to date (prior years and current period, including the change in
-- stock) sits in "Profit & Loss A/c", so Assets = Liabilities.
CREATE OR REPLACE FUNCTION fn_balance_sheet(p_company integer, p_asof date)
RETURNS TABLE (primary_group varchar, group_name varchar, ledger varchar,
               amount numeric, side text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT b.primary_group, b.group_name, b.ledger,
           ABS(b.balance)::numeric,
           CASE WHEN b.balance < 0 THEN 'Assets' ELSE 'Liabilities' END::text
    FROM fn_balances(p_company, p_asof, false) b
    WHERE b.is_revenue = 0
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
    LIMIT p_limit;
$$;

-- ------------------------------------------------------------ 9. top parties
-- A ranking: here the limit IS the question ("top 10"), so it stays.
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
      AND lg.group_path && ARRAY['cash-in-hand', 'bank accounts', 'bank od a/c', 'bank occ a/c']
    ORDER BY lg.ledger;
$$;

-- ------------------------------------------------------------ 11. GST summary
CREATE OR REPLACE FUNCTION fn_gst_summary(p_company integer, p_from date, p_to date)
RETURNS TABLE (ledger varchar, group_name varchar, amount numeric, dr_cr text)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT lg.ledger::varchar, lg.group_name::varchar,
           ABS(m.movement)::numeric,
           CASE WHEN m.movement < 0 THEN 'Dr' ELSE 'Cr' END::text
    FROM v_ledger_group lg
    JOIN fn_movement(p_company, p_from, p_to) m ON m.ledger = lg.ledger
    WHERE lg.company_id = p_company
      AND 'duties & taxes' = ANY (lg.group_path)
      AND ABS(m.movement) > 0.005
    ORDER BY ABS(m.movement) DESC;
$$;

-- ------------------------------------------------------ 12. voucher search
-- Party matches EXACTLY (case-insensitive): either the voucher's party, or any
-- accounting line on that ledger (payments often have no party_name).
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
      AND (p_party IS NULL OR p_party = ''
           OR lower(btrim(v.party_name)) = lower(btrim(p_party))
           OR EXISTS (SELECT 1 FROM trn_accounting a
                      WHERE a.company_id = v.company_id AND a.guid = v.guid
                        AND lower(btrim(a.ledger)) = lower(btrim(p_party))))
      AND (p_min_amount IS NULL OR t.amt >= p_min_amount)
    ORDER BY v.date DESC, v.voucher_number DESC
    LIMIT p_limit;
$$;

-- ------------------------------------------------- 13. party master lookup
-- EXACT name (case-insensitive): a substring match could return another
-- party's GSTIN/PAN ("Sharma Traders" vs "Anil Sharma Traders").
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
      AND lower(btrim(l.name)) = lower(btrim(p_party))
    LIMIT 1;
$$;

-- ------------------------------------------- inventory without stock values
-- True when the company has inventory movements but no closing-stock values:
-- then P&L, trial balance and balance sheet cannot include closing stock and
-- the answer must say so.
CREATE OR REPLACE FUNCTION fn_stock_gap(p_company integer)
RETURNS boolean
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM trn_inventory WHERE company_id = p_company)
       AND NOT EXISTS (SELECT 1 FROM trn_closingstock WHERE company_id = p_company);
$$;


-- ---------------------------------------------- 14. top overdue parties
-- Parties ranked by the amount that is past due. A bill is overdue once it is
-- older than the party's credit days (0 when none is set, as in Tally). Built on
-- fn_bills_outstanding with NO row limit, so totals are complete; advances are
-- not counted as overdue.
CREATE OR REPLACE FUNCTION fn_top_overdue_parties(p_company integer, p_group text,
                                                  p_asof date, p_limit integer)
RETURNS TABLE (party varchar, overdue_amount numeric, overdue_bills bigint,
               oldest_overdue_days integer, total_outstanding numeric, credit_days integer)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp AS $$
    WITH b AS (
        SELECT x.party AS pty, x.age_days AS age, x.outstanding AS amt,
               COALESCE(l.credit_days, 0) AS cd
        FROM fn_bills_outstanding(p_company, p_group, p_asof, NULL) x
        LEFT JOIN mst_ledger l ON l.company_id = p_company AND l.name = x.party
        WHERE x.kind = 'Bill'
    )
    SELECT b.pty::varchar,
           SUM(b.amt) FILTER (WHERE b.age > b.cd)::numeric,
           (COUNT(*) FILTER (WHERE b.age > b.cd))::bigint,
           (MAX(b.age) FILTER (WHERE b.age > b.cd))::integer,
           SUM(b.amt)::numeric,
           MAX(b.cd)::integer
    FROM b
    GROUP BY b.pty
    HAVING COALESCE(SUM(b.amt) FILTER (WHERE b.age > b.cd), 0) > 0.005
    ORDER BY 2 DESC, 1
    LIMIT LEAST(COALESCE(p_limit, 10), 100);
$$;
