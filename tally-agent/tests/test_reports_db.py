"""Report functions against the seeded demo company. Figures are hand-checked."""
from decimal import Decimal

import asyncpg
import pytest

from app import db
from app.reports.registry import REPORTS, ReportError, build_arguments, run_report

pytestmark = pytest.mark.usefixtures("dbready")


async def rows(report_id, params, company=1, scope=(1,)):
    async with db.company_scope(list(scope)) as con:
        return await run_report(con, REPORTS[report_id], company, params)


async def test_trial_balance_balances_to_zero():
    r = await rows("trial_balance", {"as_of": "2025-03-31"})
    assert len(r) == 15
    dr = sum(x["debit"] for x in r)
    cr = sum(x["credit"] for x in r)
    assert dr == cr == Decimal("1051000.00")


async def test_ledger_balance_is_case_insensitive():
    [r] = await rows("ledger_balance", {"ledger": "acme interiors pvt ltd", "as_of": "2025-03-31"})
    assert r["closing"] == Decimal("150000.00") and r["dr_cr"] == "Dr"


async def test_ageing_buckets():
    r = await rows("ageing_summary", {"group": "Sundry Debtors", "as_of": "2025-03-31"})
    assert {x["bucket"]: x["outstanding"] for x in r} == {
        "91-180": Decimal("177000.00"), "180+": Decimal("136000.00")}


async def test_bills_outstanding_partial_payment_and_settled_bill():
    r = await rows("bills_outstanding", {"group": "Sundry Debtors", "as_of": "2025-03-31"})
    by_ref = {x["bill_ref"]: x for x in r}
    assert "SI-001" not in by_ref                       # fully paid
    assert by_ref["SI-002"]["outstanding"] == Decimal("136000.00")   # 236000 - 100000
    assert by_ref["SI-002"]["age_days"] == 320


async def test_payables_side():
    r = await rows("bills_outstanding", {"group": "Sundry Creditors", "as_of": "2025-03-31"})
    assert [(x["party"], x["outstanding"]) for x in r] == [("Deepak Stationers", Decimal("20000.00"))]


async def test_profit_and_loss():
    [r] = await rows("profit_loss_summary", {"date_from": "2024-04-01", "date_to": "2025-03-31"})
    assert (r["total_income"], r["total_expense"], r["net_profit"]) == (
        Decimal("450000.00"), Decimal("301000.00"), Decimal("149000.00"))


async def test_ledger_statement_running_balance():
    r = await rows("ledger_statement", {"ledger": "Borealis Furnishings LLP",
                                        "date_from": "2024-04-01", "date_to": "2025-03-31"})
    assert [x["running_balance"] for x in r] == [Decimal("236000.00"), Decimal("136000.00")]


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
