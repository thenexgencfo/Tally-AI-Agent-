"""Report functions against the seeded demo company. Figures are hand-checked."""
from decimal import Decimal

import asyncpg
import pytest

from app import db
from app.reports.registry import REPORTS, ReportError, build_arguments, run_report

pytestmark = pytest.mark.usefixtures("dbready")


async def rows(report_id, params, company=1, scope=(1,)):
    async with db.company_scope(list(scope)) as con:
        result, _truncated = await run_report(con, REPORTS[report_id], company, params)
        return result


async def test_trial_balance_balances_to_zero():
    r = await rows("trial_balance", {"as_of": "2025-03-31"})
    assert len(r) == 15
    dr = sum(x["debit"] for x in r)
    cr = sum(x["credit"] for x in r)
    assert dr == cr == Decimal("1051000.00")


async def test_ledger_balance_is_case_insensitive():
    [r] = await rows("ledger_balance", {"ledger": "acme interiors pvt ltd", "as_of": "2025-03-31"})
    assert r["closing"] == Decimal("150000.00") and r["dr_cr"] == "Dr"


async def test_ageing_buckets_include_opening_bills():
    r = await rows("ageing_summary", {"group": "Sundry Debtors", "as_of": "2025-03-31"})
    # SI-003 177000; SI-002 136000 + Acme's opening bill OB-2023-17 150000 (pre-books)
    assert {x["bucket"]: x["outstanding"] for x in r} == {
        "91-180": Decimal("177000.00"), "180+": Decimal("286000.00")}
    # Net receivables equal the debtors' ledger balances (150000+136000+177000).
    assert sum(x["outstanding"] for x in r) == Decimal("463000.00")


async def test_bills_outstanding_partial_payment_and_settled_bill():
    r = await rows("bills_outstanding", {"group": "Sundry Debtors", "as_of": "2025-03-31"})
    by_ref = {x["bill_ref"]: x for x in r}
    assert "SI-001" not in by_ref                       # fully paid
    assert by_ref["OB-2023-17"]["outstanding"] == Decimal("150000.00")   # opening bill
    assert by_ref["SI-002"]["outstanding"] == Decimal("136000.00")   # 236000 - 100000
    assert by_ref["SI-002"]["age_days"] == 320


async def test_payables_side():
    r = await rows("bills_outstanding", {"group": "Sundry Creditors", "as_of": "2025-03-31"})
    assert [(x["party"], x["outstanding"], x["kind"]) for x in r] == [
        ("Deepak Stationers", Decimal("20000.00"), "Bill")]


async def test_profit_and_loss():
    [r] = await rows("profit_loss_summary", {"date_from": "2024-04-01", "date_to": "2025-03-31"})
    assert (r["total_income"], r["total_expense"], r["net_profit"]) == (
        Decimal("450000.00"), Decimal("301000.00"), Decimal("149000.00"))


async def test_ledger_statement_running_balance():
    r = await rows("ledger_statement", {"ledger": "Borealis Furnishings LLP",
                                        "date_from": "2024-04-01", "date_to": "2025-03-31"})
    assert [(x["running_balance"], x["dr_cr"]) for x in r] == [
        (Decimal("236000.00"), "Dr"), (Decimal("136000.00"), "Dr")]


async def test_gst_summary():
    r = await rows("gst_summary", {"date_from": "2024-04-01", "date_to": "2025-03-31"})
    assert {x["ledger"]: x["amount"] for x in r} == {
        "Output IGST": Decimal("36000.00"), "Output CGST": Decimal("22500.00"),
        "Output SGST": Decimal("22500.00")}


# ---------------------------------------------------------------- isolation
async def test_rls_blocks_reads_without_scope():
    pool = await db.get_pool()
    async with pool.acquire() as con:
        assert await con.fetchval("SELECT count(*) FROM tally.mst_ledger") == 0


async def test_cross_company_returns_nothing():
    # User scoped to company 1 asks for company 2's trial balance.
    assert await rows("trial_balance", {"as_of": "2025-03-31"}, company=2, scope=(1,)) == []


async def test_scope_is_transaction_local_and_does_not_leak_to_pool():
    async with db.company_scope([1]) as con:
        assert await con.fetchval("SELECT count(*) FROM tally.mst_company") == 1
    pool = await db.get_pool()
    async with pool.acquire() as con:   # likely the same physical connection
        assert await con.fetchval("SELECT count(*) FROM tally.mst_company") == 0


async def test_app_role_cannot_write():
    pool = await db.get_pool()
    async with pool.acquire() as con:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("UPDATE tally.mst_ledger SET opening_balance = 0")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.execute("DELETE FROM tally.trn_voucher")


async def test_app_role_cannot_read_audit_log():
    pool = await db.get_pool()
    async with pool.acquire() as con:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.fetch("SELECT * FROM audit.query_log")


async def test_audit_log_is_append_only(owner_con):
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await owner_con.execute("UPDATE audit.query_log SET outcome = 'tampered'")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await owner_con.execute("DELETE FROM audit.query_log")


async def test_every_view_is_security_invoker(owner_con):
    bad = await owner_con.fetch("""
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'tally' AND c.relkind = 'v'
          AND NOT COALESCE((SELECT option_value::boolean FROM pg_options_to_table(c.reloptions)
                            WHERE option_name = 'security_invoker'), false)""")
    assert bad == []


# ------------------------------------------------------------ parameters
async def test_sql_injection_in_text_param_is_inert():
    evil = "Sales'; DROP TABLE tally.trn_voucher; --"
    assert await rows("day_book", {"date_from": "2024-04-01", "date_to": "2025-03-31",
                                   "voucher_type": evil}) == []
    r = await rows("day_book", {"date_from": "2024-04-01", "date_to": "2025-03-31"})
    assert len(r) == 9   # table intact


def test_unknown_and_missing_params_rejected():
    with pytest.raises(ReportError, match="unknown parameter"):
        build_arguments(REPORTS["trial_balance"], {"as_of": "2025-03-31", "sql": "x"})
    with pytest.raises(ReportError, match="missing required"):
        build_arguments(REPORTS["ledger_balance"], {"as_of": "2025-03-31"})
    with pytest.raises(ReportError, match="not a valid date"):
        build_arguments(REPORTS["trial_balance"], {"as_of": "yesterday-ish"})


def test_role_gating():
    assert not REPORTS["party_details"].allowed_for("junior")
    assert REPORTS["party_details"].allowed_for("senior")


# ------------------------------------------------------------------ ingest
async def test_loader_ingest_round_trip(owner_con):
    from pathlib import Path
    ddl = Path("sql/loader_schema_pg.sql").read_text().replace(':"schema"', "loader_pytest")
    await owner_con.execute("DROP SCHEMA IF EXISTS loader_pytest CASCADE")
    await owner_con.execute(ddl)
    await owner_con.execute("""
        SET search_path = loader_pytest;
        INSERT INTO mst_group VALUES ('g1','Sundry Debtors','Primary','Sundry Debtors',0,1,1,0,1),
                                     ('g2','Sales Accounts','Primary','Sales Accounts',1,0,1,1,2);
        INSERT INTO mst_ledger (guid,name,parent,is_revenue,opening_balance,mobile,bill_credit_period)
          VALUES ('l1','Kumar Traders','Sundry Debtors',0,0,'9876543210',30),('l2','Sales','Sales Accounts',1,0,'',0);
        INSERT INTO trn_voucher (guid,date,voucher_type,voucher_number,party_name) VALUES ('v1','2024-06-01','Sales','S-1','Kumar Traders');
        INSERT INTO trn_accounting (guid,ledger,amount) VALUES ('v1','Kumar Traders',-5000),('v1','Sales',5000);
        INSERT INTO trn_bill (guid,ledger,name,amount,billtype) VALUES ('v1','Kumar Traders','S-1',-5000,'New Ref');
        SET search_path = public;""")
    try:
        n1 = await owner_con.fetchval("SELECT tally.fn_ingest(90, 'loader_pytest', 'Pytest Co')")
        n2 = await owner_con.fetchval("SELECT tally.fn_ingest(90, 'loader_pytest', 'Pytest Co')")
        assert n1 == n2 == 8   # idempotent: a re-run replaces, never duplicates
        r = await rows("trial_balance", {"as_of": "2025-03-31"}, company=90, scope=(90,))
        assert sum(x["debit"] for x in r) == sum(x["credit"] for x in r) == Decimal("5000.00")
        [b] = await rows("bills_outstanding", {"group": "Sundry Debtors", "as_of": "2025-03-31"},
                         company=90, scope=(90,))
        assert str(b["bill_date"]) == "2024-06-01"   # loader has no bill date -> voucher date
    finally:
        await owner_con.execute("DELETE FROM tally.mst_company WHERE company_id = 90")
        await owner_con.execute("DROP SCHEMA IF EXISTS loader_pytest CASCADE")


async def test_app_role_cannot_ingest():
    pool = await db.get_pool()
    async with pool.acquire() as con:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await con.fetchval("SELECT tally.fn_ingest(1, 'loader_pytest', 'x')")


async def test_audit_log_cannot_be_truncated(owner_con):
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await owner_con.execute("TRUNCATE audit.query_log")


async def test_loader_role_provisioning_rejects_bad_names():
    import subprocess
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-q", "-d", "tally", "-v", "role=evil; drop",
                        "-v", "pass=x", "-f", "sql/admin/add_loader_company.sql"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "loader_c" in r.stderr


# ============================================================ review fixes
async def _loader_schema(owner_con, name):
    from pathlib import Path
    ddl = Path("sql/loader_schema_pg.sql").read_text().replace(':"schema"', name)
    await owner_con.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    await owner_con.execute(ddl)
    await owner_con.execute("SET search_path = public")


@pytest.mark.parametrize("attack", [
    # a view whose code would run with the ingest role's rights
    "DROP TABLE {s}.mst_group; CREATE VIEW {s}.mst_group AS SELECT 'g'::varchar guid, "
    "current_user::varchar name, ''::varchar parent, ''::varchar primary_group, 0::smallint is_revenue, "
    "0::smallint is_deemedpositive, 0::smallint is_reserved, 0::smallint affects_gross_profit, 0 sort_position",
    # a column of a loader-defined type (domain / custom cast path)
    "CREATE DOMAIN {s}.evil AS varchar; ALTER TABLE {s}.mst_group ALTER COLUMN name TYPE {s}.evil",
    # a child table hiding behind inheritance
    "CREATE TABLE {s}.sneaky () INHERITS ({s}.mst_ledger)",
])
async def test_ingest_refuses_anything_but_plain_builtin_tables(owner_con, attack):
    await _loader_schema(owner_con, "loader_attack")
    try:
        await owner_con.execute(attack.format(s="loader_attack"))
        with pytest.raises(asyncpg.PostgresError, match="refusing to ingest"):
            await owner_con.fetchval("SELECT tally.fn_ingest(91, 'loader_attack', 'Attack Co')")
    finally:
        await owner_con.execute("DROP SCHEMA IF EXISTS loader_attack CASCADE")
        await owner_con.execute("DELETE FROM tally.mst_company WHERE company_id = 91")


@pytest.fixture
async def co600(owner_con):
    """A second realistic company: advances, an opening bill, 250 small invoices,
    a stock ledger with closing values, and a second financial year."""
    c = owner_con
    await c.execute("DELETE FROM tally.mst_company WHERE company_id = 600")
    await c.execute("""
      INSERT INTO tally.mst_company (company_id, guid, name, fy_from, fy_to) VALUES
        (600,'c600','Kapoor Test Co','2024-04-01','2025-03-31');
      INSERT INTO tally.mst_group (company_id,guid,name,parent,primary_group,is_revenue) VALUES
        (600,'a','Current Assets','','Current Assets',0),(600,'b','Sundry Debtors','Current Assets','Current Assets',0),
        (600,'c','Bank Accounts','Current Assets','Current Assets',0),(600,'d','Stock-in-Hand','Current Assets','Current Assets',0),
        (600,'e','Sales Accounts','','Sales Accounts',1),(600,'f','Capital Account','','Capital Account',0);
      INSERT INTO tally.mst_ledger (company_id,guid,name,parent,opening_balance) VALUES
        (600,'l1','Gupta Agencies','Sundry Debtors',-10000),(600,'l2','Kapoor Exports','Sundry Debtors',0),
        (600,'l3','Bank','Bank Accounts',-40000),(600,'l4','Closing Stock','Stock-in-Hand',-10000),
        (600,'l5','Sales','Sales Accounts',0),(600,'l6','Capital','Capital Account',60000);
      INSERT INTO tally.mst_opening_bill (company_id,line_no,ledger,name,bill_date,amount) VALUES
        (600,1,'Gupta Agencies','OB-1','2024-03-01',-10000);
      INSERT INTO tally.trn_closingstock (company_id,line_no,ledger,stock_date,stock_value) VALUES
        (600,1,'Closing Stock','2025-03-31',25000);
      -- Kapoor pays a 50,000 advance before any invoice
      INSERT INTO tally.trn_voucher (company_id,guid,date,voucher_type,voucher_number,party_name) VALUES
        (600,'adv','2025-03-20','Receipt','R-1','Kapoor Exports');
      INSERT INTO tally.trn_accounting (company_id,guid,line_no,ledger,amount) VALUES
        (600,'adv',1,'Bank',-50000),(600,'adv',2,'Kapoor Exports',50000);
      INSERT INTO tally.trn_bill (company_id,guid,line_no,ledger,name,amount,billtype) VALUES
        (600,'adv',1,'Kapoor Exports','ADV-1',50000,'Advance');
    """)
    # 250 invoices of 1,000 to Gupta in FY 2024-25 -> more than any old 200-row cap
    await c.execute("""
      INSERT INTO tally.trn_voucher (company_id,guid,date,voucher_type,voucher_number,party_name)
        SELECT 600,'s'||i,'2024-04-01'::date + (i % 300),'Sales','S-'||lpad(i::text,3,'0'),'Gupta Agencies'
        FROM generate_series(1,250) i;
      INSERT INTO tally.trn_accounting (company_id,guid,line_no,ledger,amount)
        SELECT 600,'s'||i,1,'Gupta Agencies',-1000 FROM generate_series(1,250) i
        UNION ALL SELECT 600,'s'||i,2,'Sales',1000 FROM generate_series(1,250) i;
      INSERT INTO tally.trn_bill (company_id,guid,line_no,ledger,name,amount,billtype)
        SELECT 600,'s'||i,1,'Gupta Agencies','S-'||lpad(i::text,3,'0'),-1000,'New Ref' FROM generate_series(1,250) i;
    """)
    yield 600
    await c.execute("DELETE FROM tally.mst_company WHERE company_id = 600")


async def test_long_ledger_totals_are_complete(co600):
    r = await rows("ledger_statement", {"ledger": "Gupta Agencies", "date_from": "2024-04-01",
                                        "date_to": "2025-03-31"}, company=600, scope=(600,))
    assert len(r) == 250
    assert (r[-1]["running_balance"], r[-1]["dr_cr"]) == (Decimal("260000.00"), "Dr")  # 10k opening + 250k
    b = await rows("bills_outstanding", {"group": "Sundry Debtors", "as_of": "2025-03-31"},
                   company=600, scope=(600,))
    assert sum(x["outstanding"] for x in b) == Decimal("210000.00")   # 260k due - 50k advance
    v = await rows("voucher_search", {"party": "Gupta Agencies", "date_from": "2024-04-01",
                                      "date_to": "2025-03-31"}, company=600, scope=(600,))
    assert len(v) == 250


async def test_truncation_is_reported_not_hidden(co600):
    async with db.company_scope([600]) as con:
        r, truncated = await run_report(con, REPORTS["day_book"], 600,
                                        {"date_from": "2024-04-01", "date_to": "2025-03-31"}, max_rows=10)
    assert len(r) == 10 and truncated


async def test_advance_reduces_receivables(co600):
    a = await rows("ageing_summary", {"group": "Sundry Debtors", "as_of": "2025-03-31"},
                   company=600, scope=(600,))
    by = {x["bucket"]: x["outstanding"] for x in a}
    assert by["Advance"] == Decimal("-50000.00")
    assert sum(by.values()) == Decimal("210000.00")
    b = await rows("bills_outstanding", {"group": "Sundry Debtors", "as_of": "2025-03-31"},
                   company=600, scope=(600,))
    ob = [x for x in b if x["bill_ref"] == "OB-1"]
    assert [(x["outstanding"], x["bucket"], x["kind"]) for x in ob] == [(Decimal("10000.00"), "180+", "Bill")]
    adv = [x for x in b if x["kind"] == "Advance"]
    assert [(x["party"], x["outstanding"]) for x in adv] == [("Kapoor Exports", Decimal("-50000.00"))]


async def test_stock_and_fy_boundaries_keep_books_balanced(co600):
    pl = await rows("profit_loss", {"date_from": "2024-04-01", "date_to": "2025-03-31"},
                    company=600, scope=(600,))
    by = {x["ledger"]: (x["amount"], x["section"]) for x in pl}
    assert by["Opening Stock"] == (Decimal("10000.00"), "Expense")
    assert by["Closing Stock"] == (Decimal("25000.00"), "Income")
    [s] = await rows("profit_loss_summary", {"date_from": "2024-04-01", "date_to": "2025-03-31"},
                     company=600, scope=(600,))
    assert s["net_profit"] == Decimal("265000.00")    # 250k sales + 25k closing - 10k opening
    for as_of in ("2025-03-31", "2026-03-31"):
        bs = await rows("balance_sheet", {"as_of": as_of}, company=600, scope=(600,))
        assets = sum(x["amount"] for x in bs if x["side"] == "Assets")
        liab = sum(x["amount"] for x in bs if x["side"] == "Liabilities")
        assert assets == liab, as_of
        tb = await rows("trial_balance", {"as_of": as_of}, company=600, scope=(600,))
        assert sum(x["debit"] for x in tb) == sum(x["credit"] for x in tb), as_of
    tb26 = await rows("trial_balance", {"as_of": "2026-03-31"}, company=600, scope=(600,))
    names = {x["ledger"]: x for x in tb26}
    assert "Sales" not in names                         # new FY: last year's sales not repeated
    assert names["Profit & Loss A/c"]["credit"] == Decimal("265000.00")   # carried forward


async def test_party_details_requires_exact_name():
    assert await rows("party_details", {"party": "Acme"}) == []
    [r] = await rows("party_details", {"party": "acme interiors pvt ltd"})
    assert r["ledger"] == "Acme Interiors Pvt Ltd"


async def test_ingest_loads_opening_bills_and_closing_stock(owner_con):
    await _loader_schema(owner_con, "loader_ob")
    try:
        await owner_con.execute("""
          INSERT INTO loader_ob.mst_group VALUES ('g0','Current Assets','','Current Assets',0,1,1,0,1),
                                                 ('g1','Sundry Debtors','Current Assets','Current Assets',0,1,1,0,2);
          INSERT INTO loader_ob.mst_ledger (guid,name,parent,opening_balance) VALUES ('l1','Old Client','Sundry Debtors',-7000);
          INSERT INTO loader_ob.mst_opening_bill_allocation VALUES ('Old Client',-7000,'2024-01-10','INV-OLD',30,0);
          INSERT INTO loader_ob.trn_closingstock_ledger VALUES ('Closing Stock','2025-03-31',1234);""")
        await owner_con.fetchval("SELECT tally.fn_ingest(92, 'loader_ob', 'OB Co')")
        [b] = await rows("bills_outstanding", {"group": "Sundry Debtors", "as_of": "2025-03-31"},
                         company=92, scope=(92,))
        assert (b["bill_ref"], b["outstanding"], str(b["bill_date"])) == ("INV-OLD", Decimal("7000.00"), "2024-01-10")
        assert await owner_con.fetchval("SELECT count(*) FROM tally.trn_closingstock WHERE company_id=92") == 1
    finally:
        await owner_con.execute("DELETE FROM tally.mst_company WHERE company_id = 92")
        await owner_con.execute("DROP SCHEMA IF EXISTS loader_ob CASCADE")
