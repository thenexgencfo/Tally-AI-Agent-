"""
The report registry: the complete list of things the agent can ask the
database for.

This is the security boundary. The model never emits SQL. It emits a
report_id from this registry plus typed parameters, which are bound as query
parameters. A report that is not in this table cannot be run, and adding one
is a reviewed code change rather than a model decision.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Sequence

import asyncpg

ParamType = Literal["date", "text", "int", "numeric", "ledger", "group"]
Role = Literal["junior", "senior", "partner"]
ROLE_ORDER: dict[str, int] = {"junior": 1, "senior": 2, "partner": 3}


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType
    required: bool = True
    default: Any = None
    description: str = ""
    # Internal parameters are never offered to the model or the form and are
    # always left at their default (e.g. row limits: NULL = all rows, so totals
    # are never computed from a truncated list).
    internal: bool = False

    @property
    def is_name(self) -> bool:
        """Carries a party/ledger name, so it arrives tokenized and must be
        resolved back to the real value before it reaches the database."""
        return self.type == "ledger"


@dataclass(frozen=True)
class ReportSpec:
    id: str
    title: str
    description: str
    function: str                       # tally.fn_*
    params: tuple[ParamSpec, ...]
    min_role: Role = "junior"
    examples: tuple[str, ...] = ()
    # Columns the answer should quote. Used to keep answers grounded.
    value_columns: tuple[str, ...] = ()
    sensitive: bool = False             # exposes identifiers (PAN/GSTIN/bank)

    def allowed_for(self, role: str) -> bool:
        return ROLE_ORDER.get(role, 0) >= ROLE_ORDER[self.min_role]


_D = ParamSpec


REPORTS: dict[str, ReportSpec] = {
    r.id: r
    for r in [
        ReportSpec(
            id="trial_balance",
            title="Trial Balance",
            description="Closing debit and credit of every ledger as on a date.",
            function="tally.fn_trial_balance",
            params=(_D("as_of", "date", description="Balances as on this date"),),
            value_columns=("debit", "credit"),
            examples=("trial balance as on 31 March 2025",),
        ),
        ReportSpec(
            id="ledger_balance",
            title="Ledger Balance",
            description="Closing balance of one named ledger or party as on a date.",
            function="tally.fn_ledger_balance",
            params=(
                _D("ledger", "ledger", description="Exact ledger or party name"),
                _D("as_of", "date"),
            ),
            value_columns=("closing", "dr_cr"),
            examples=("what is the balance of Acme Interiors", "how much does X owe"),
        ),
        ReportSpec(
            id="ledger_statement",
            title="Ledger Statement",
            description=(
                "Every transaction on one ledger between two dates, with a "
                "running balance."
            ),
            function="tally.fn_ledger_statement",
            params=(
                _D("ledger", "ledger"),
                _D("date_from", "date"),
                _D("date_to", "date"),
                _D("limit", "int", required=False, default=None, internal=True),
            ),
            value_columns=("debit", "credit", "running_balance"),
            examples=("show me the ledger of X for this year",),
        ),
        ReportSpec(
            id="bills_outstanding",
            title="Bills Outstanding",
            description=(
                "Unpaid bill-by-bill detail with age in days and ageing bucket; "
                "advances shown as negative. group is 'Sundry Debtors' for "
                "receivables, 'Sundry Creditors' for payables."
            ),
            function="tally.fn_bills_outstanding",
            params=(
                _D("group", "group", default="Sundry Debtors"),
                _D("as_of", "date"),
                _D("limit", "int", required=False, default=None, internal=True),
            ),
            value_columns=("outstanding", "age_days", "bucket"),
            examples=("which invoices are overdue", "outstanding bills of creditors"),
        ),
        ReportSpec(
            id="top_overdue_parties",
            title="Top Overdue Parties",
            description=(
                "Parties ranked by the amount past due (older than the party's credit days). "
                "group is 'Sundry Creditors' for what we owe, 'Sundry Debtors' for what is owed "
                "to us. limit is how many parties (default 10)."
            ),
            function="tally.fn_top_overdue_parties",
            params=(
                _D("group", "group", default="Sundry Debtors"),
                _D("as_of", "date"),
                _D("limit", "int", required=False, default=10),
            ),
            value_columns=("overdue_amount", "overdue_bills", "oldest_overdue_days"),
            examples=("top 10 creditors by overdue amount", "which customers owe us the most overdue",
                      "biggest overdue suppliers this month"),
        ),
        ReportSpec(
            id="ageing_summary",
            title="Ageing Summary",
            description="Totals per ageing bucket (0-30, 31-60, 61-90, 91-180, 180+).",
            function="tally.fn_ageing_summary",
            params=(
                _D("group", "group", default="Sundry Debtors"),
                _D("as_of", "date"),
            ),
            value_columns=("outstanding", "bill_count"),
            examples=("receivables ageing", "how old is our debt"),
        ),
        ReportSpec(
            id="profit_loss",
            title="Profit and Loss detail",
            description="Income and expense ledgers with amounts for a period.",
            function="tally.fn_profit_loss",
            params=(_D("date_from", "date"), _D("date_to", "date")),
            value_columns=("amount",),
            examples=("profit and loss for the year", "what were our expenses"),
        ),
        ReportSpec(
            id="profit_loss_summary",
            title="Profit and Loss summary",
            description="Total income, total expense and net profit for a period.",
            function="tally.fn_profit_loss_summary",
            params=(_D("date_from", "date"), _D("date_to", "date")),
            value_columns=("total_income", "total_expense", "net_profit"),
            examples=("what is our net profit", "did we make money this year"),
        ),
        ReportSpec(
            id="balance_sheet",
            title="Balance Sheet",
            description="Assets and liabilities by group as on a date.",
            function="tally.fn_balance_sheet",
            params=(_D("as_of", "date"),),
            value_columns=("amount",),
            examples=("balance sheet as on 31 March",),
        ),
        ReportSpec(
            id="day_book",
            title="Day Book",
            description=(
                "Vouchers entered between two dates, newest first. Optionally "
                "filter to one voucher type such as Sales, Receipt, Payment."
            ),
            function="tally.fn_day_book",
            params=(
                _D("date_from", "date"),
                _D("date_to", "date"),
                _D("voucher_type", "text", required=False, default=None),
                _D("limit", "int", required=False, default=None, internal=True),
            ),
            value_columns=("amount",),
            examples=("what was entered yesterday", "show sales vouchers in May"),
        ),
        ReportSpec(
            id="top_parties",
            title="Top Parties",
            description="Parties ranked by total voucher value in a period.",
            function="tally.fn_top_parties",
            params=(
                _D("date_from", "date"),
                _D("date_to", "date"),
                _D("voucher_type", "text", required=False, default="Sales"),
                _D("limit", "int", required=False, default=10),
            ),
            value_columns=("total_amount", "voucher_count"),
            examples=("who are our biggest customers",),
        ),
        ReportSpec(
            id="cash_bank_balance",
            title="Cash and Bank Balances",
            description="Balance of every cash and bank ledger as on a date.",
            function="tally.fn_cash_bank_balance",
            params=(_D("as_of", "date"),),
            value_columns=("balance", "dr_cr"),
            examples=("how much cash do we have", "bank balance"),
        ),
        ReportSpec(
            id="gst_summary",
            title="GST Ledger Summary",
            description="Movement on every Duties & Taxes ledger for a period.",
            function="tally.fn_gst_summary",
            params=(_D("date_from", "date"), _D("date_to", "date")),
            value_columns=("amount", "dr_cr"),
            examples=("gst liability for the quarter", "output tax"),
        ),
        ReportSpec(
            id="voucher_search",
            title="Voucher Search",
            description="Find vouchers by party, date range and minimum amount.",
            function="tally.fn_voucher_search",
            params=(
                _D("party", "ledger", required=False, default=None),
                _D("date_from", "date"),
                _D("date_to", "date"),
                _D("min_amount", "numeric", required=False, default=None),
                _D("limit", "int", required=False, default=None, internal=True),
            ),
            value_columns=("amount",),
            examples=("find payments above 1 lakh", "vouchers for X in June"),
        ),
        ReportSpec(
            id="party_details",
            title="Party Master Details",
            description=(
                "Master data for a party: GSTIN, PAN, email, phone, state, "
                "credit limit and credit days."
            ),
            function="tally.fn_party_details",
            params=(_D("party", "ledger"),),
            min_role="senior",
            sensitive=True,
            value_columns=("credit_limit", "credit_days"),
            examples=("what is the GST number of X", "credit days for X"),
        ),
    ]
}


def catalogue_for_role(role: str) -> list[ReportSpec]:
    return [r for r in REPORTS.values() if r.allowed_for(role)]


# ------------------------------------------------------------------ runner
def _coerce(spec: ParamSpec, value: Any) -> Any:
    if value is None:
        return None
    if spec.type == "date":
        if isinstance(value, dt.date):
            return value
        return dt.date.fromisoformat(str(value)[:10])
    if spec.type == "int":
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("not finite")
        return int(value)
    if spec.type == "numeric":
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError("not finite")
        return d
    return str(value)


class ReportError(Exception):
    pass


def build_arguments(spec: ReportSpec, params: dict[str, Any]) -> list[Any]:
    """Validate and order parameters. Unknown keys are rejected, not ignored."""
    known = {p.name for p in spec.params if not p.internal}
    unknown = set(params) - known
    if unknown:
        raise ReportError(f"unknown parameter(s): {', '.join(sorted(unknown))}")

    args: list[Any] = []
    for p in spec.params:
        raw = p.default if p.internal else params.get(p.name, p.default)
        if raw is None and p.required:
            raise ReportError(f"missing required parameter '{p.name}'")
        try:
            args.append(_coerce(p, raw))
        except (ValueError, TypeError, OverflowError, ArithmeticError) as exc:
            raise ReportError(f"parameter '{p.name}' is not a valid {p.type}") from exc
    return args


async def run_report(
    con: asyncpg.Connection,
    spec: ReportSpec,
    company_id: int,
    params: dict[str, Any],
    *,
    max_rows: int = 20_000,
) -> tuple[list[dict], bool]:
    """Returns (rows, truncated). truncated=True means the report had more than
    max_rows rows; callers must say so, because totals then cover a subset."""
    args = build_arguments(spec, params)
    placeholders = ", ".join(f"${i+1}" for i in range(len(args) + 1))
    # spec.function comes from this module's fixed registry, never from input.
    sql = f"SELECT * FROM {spec.function}({placeholders}) LIMIT {int(max_rows) + 1}"
    rows: Sequence[asyncpg.Record] = await con.fetch(sql, int(company_id), *args)
    truncated = len(rows) > max_rows
    return [dict(r) for r in rows[:max_rows]], truncated
