"""
Planner: masked question -> (report_id, parameters).

The planner is the ONLY step where the model influences what runs, and it
sees nothing but the user's masked question and the report catalogue. It
never sees database content, so text planted in a voucher narration cannot
change which report is executed.

Its output is untrusted: the report must exist and be allowed for the role,
unknown parameters are dropped, values are type-checked, and dates named in
the question are resolved by code and override the model.

If the model is down or returns garbage, a keyword router takes over. The
system degrades to "less clever", never to "unavailable".
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..masking.engine import TOKEN_RE
from ..reports.registry import REPORTS, ReportSpec, catalogue_for_role
from .dates import Period, resolve_period
from .llm import LLM, LLMUnavailable

DEBTORS = "Sundry Debtors"
CREDITORS = "Sundry Creditors"
_PAYABLE = re.compile(r"payable|creditor|supplier|vendor|we owe|(?:we|i)\s+(?:have|need)\s+to\s+pay"
                      r"|purchase bills?|bills? (?:we|to) pay")
_RECEIVABLE = re.compile(r"receivable|debtor|customer|client|owes? us|owed to us|to receive|collect")


@dataclass
class Plan:
    report_id: str | None
    params: dict[str, Any] = field(default_factory=dict)
    source: str = "rules"                 # llm | rules | direct
    clarification: str | None = None
    warnings: list[str] = field(default_factory=list)
    period: Period | None = None
    wants_live: bool = False


class PlanDenied(Exception):
    pass


# ------------------------------------------------------------------ prompt
def _catalogue_text(reports: list[ReportSpec]) -> str:
    lines = []
    for r in reports:
        ps = ", ".join(
            f"{p.name}:{p.type}{'' if p.required else '?'}" for p in r.params if not p.internal
        )
        lines.append(f"- {r.id}({ps}) -- {r.description}")
    return "\n".join(lines)


def build_system_prompt(reports: list[ReportSpec], today: dt.date) -> str:
    from .dates import fy_bounds

    fy_from, fy_to = fy_bounds(today)
    return f"""You route questions about a company's Tally accounts to ONE report.
You never calculate, never answer the question yourself, never write SQL.
Reply with JSON only.

Rules:
- Pick report_id from the list below, or "none" if no report fits.
- Placeholders such as [PARTY_1] stand for real names. Copy them EXACTLY into
  parameters that take a ledger name. Never invent a name.
- Dates are YYYY-MM-DD. Indian financial year runs 1 April to 31 March.
  Today is {today.isoformat()}. Current FY: {fy_from.isoformat()} to {fy_to.isoformat()}.
- For "group" use "{DEBTORS}" (receivables, customers) or "{CREDITORS}" (payables, suppliers).
- If report_id is "none", put a short question back to the user in "clarification".

Reports:
{_catalogue_text(reports)}"""


def plan_schema(reports: list[ReportSpec]) -> dict:
    return {
        "type": "object",
        "properties": {
            "report_id": {"type": "string", "enum": [r.id for r in reports] + ["none"]},
            "params": {"type": "object"},
            "clarification": {"type": "string"},
        },
        "required": ["report_id", "params"],
    }


# ---------------------------------------------------------------- planner
class Planner:
    def __init__(self, llm: LLM | None) -> None:
        self._llm = llm

    async def plan(self, masked_question: str, role: str, today: dt.date) -> Plan:
        allowed = catalogue_for_role(role)
        period = resolve_period(masked_question, today)
        warnings: list[str] = []

        if self._llm is not None:
            try:
                raw = await self._llm.chat_json(
                    build_system_prompt(allowed, today),
                    masked_question,
                    plan_schema(allowed),
                )
                plan = self._from_model(raw, masked_question, role, period)
                plan.source = "llm"
                return plan
            except PlanDenied:
                raise
            except (LLMUnavailable, ValueError, KeyError, TypeError) as exc:
                warnings.append(
                    "The language model was unavailable or gave an unusable "
                    f"answer ({type(exc).__name__}); used keyword routing instead."
                )

        plan = rules_plan(masked_question, role, period)
        plan.warnings = warnings + plan.warnings
        return plan

    # -------------------------------------------------- validate model output
    def _from_model(self, raw: dict, question: str, role: str, period: Period) -> Plan:
        report_id = str(raw.get("report_id", "")).strip()
        if report_id == "none":
            return Plan(None, clarification=str(raw.get("clarification") or "")[:300]
                        or "Could you rephrase? I could not match that to a report.",
                        period=period)
        spec = REPORTS.get(report_id)
        if spec is None:
            raise ValueError(f"model chose unknown report {report_id!r}")
        if not spec.allowed_for(role):
            raise PlanDenied(f"report '{report_id}' requires role {spec.min_role}")

        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params is not an object")
        return finalize(spec, params, question, period)


# ------------------------------------------------------------- finalising
def _tokens(question: str, entity: str = "PARTY") -> list[str]:
    return [m.group(0) for m in TOKEN_RE.finditer(question) if m.group(1) == entity]


def finalize(spec: ReportSpec, params: dict[str, Any], question: str, period: Period) -> Plan:
    """Normalise parameters for one report. Shared by the model and rules paths."""
    warnings: list[str] = []
    known = {p.name: p for p in spec.params if not p.internal}
    dropped = sorted(set(params) - set(known))
    if dropped:
        warnings.append(f"Ignored unexpected parameter(s): {', '.join(dropped)}")
    out: dict[str, Any] = {k: v for k, v in params.items() if k in known and v not in ("", None)}

    party_tokens = _tokens(question)
    for p in spec.params:
        if p.internal:
            continue
        if p.type == "ledger":
            val = out.get(p.name)
            # A ledger parameter must be one of the placeholders from the question.
            # Anything else is a name the model made up, or real text that slipped
            # past masking -- use the question's own placeholder if there is one.
            if val is not None and not TOKEN_RE.fullmatch(str(val).strip()):
                val = None
            if val is None and party_tokens:
                val = party_tokens[0]
            if val is None:
                if p.required:
                    return Plan(
                        None,
                        clarification=(
                            f"Which party or ledger do you mean for {spec.title}? "
                            "Please give the name as it appears in Tally."
                        ),
                        period=period,
                    )
                out.pop(p.name, None)
            else:
                out[p.name] = str(val).strip()
        elif p.type == "group":
            val = str(out.get(p.name) or "").lower()
            ql = question.lower()
            if _RECEIVABLE.search(ql):
                out[p.name] = DEBTORS
            elif _PAYABLE.search(ql) or "creditor" in val:
                out[p.name] = CREDITORS
            else:
                out[p.name] = DEBTORS
        elif p.type == "date":
            # Code-resolved dates win whenever the question named a period.
            code_value = {
                "as_of": period.as_of, "date_from": period.date_from, "date_to": period.date_to,
            }.get(p.name)
            model_value = _parse_date(out.get(p.name))
            if period.explicit or model_value is None:
                out[p.name] = code_value.isoformat() if code_value else None
            else:
                out[p.name] = model_value.isoformat()
        elif p.type == "int" and p.name in out:
            try:
                out[p.name] = max(1, min(int(out[p.name]), 1000))
            except (TypeError, ValueError, OverflowError):
                out.pop(p.name)
        elif p.type == "numeric" and p.name in out:
            try:
                val = float(str(out[p.name]).replace(",", ""))
                if not math.isfinite(val):
                    raise ValueError("not finite")
                out[p.name] = val
            except (TypeError, ValueError):
                out.pop(p.name)

    if "date_from" in out and "date_to" in out and out["date_from"] > out["date_to"]:
        out["date_from"], out["date_to"] = out["date_to"], out["date_from"]

    if period.warning:
        warnings.append(period.warning)
    return Plan(spec.id, out, warnings=warnings, period=period,
                wants_live=bool(re.search(r"\b(right now|live|this moment|real[- ]?time|latest)\b",
                                          question.lower())))


def _parse_date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    try:
        d = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return d if dt.date(1990, 1, 1) <= d <= dt.date(2100, 1, 1) else None


# -------------------------------------------------------- keyword router
_RULES_WITH_PARTY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"statement|transactions?|entries|ledger (?:of|for)|account of"), "ledger_statement"),
    # Balance intent outranks identifier words: "X's PAN is ..., check balance".
    (re.compile(r"balance|\bowes?\b|outstanding|\bdue\b|pending|receivable|payable|how much"),
     "ledger_balance"),
    (re.compile(r"gstin|gst (?:no|number)|\bpan\b|email|phone|mobile|contact|credit (?:days|limit|period)|address"),
     "party_details"),
    (re.compile(r"vouchers?|invoices?|payments?|receipts?|bills? (?:of|from|to)"), "voucher_search"),
]
_RULES_GENERAL: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"trial balance|\btb\b"), "trial_balance"),
    (re.compile(r"balance ?sheet"), "balance_sheet"),
    (re.compile(r"ageing|aging|how old|age-?wise|bucket"), "ageing_summary"),
    (re.compile(r"overdue|outstanding|unpaid|pending|receivables?|payables?|who owes|debtors|creditors"
                r"|yet to (?:pay|receive)|(?:need|have) to pay|to be (?:paid|received)|\bdues?\b"),
     "bills_outstanding"),
    (re.compile(r"\bgst\b|cgst|sgst|igst|duties|output tax|input tax|tax liability"), "gst_summary"),
    (re.compile(r"cash|bank balance|bank accounts?|liquidity"), "cash_bank_balance"),
    (re.compile(r"top|biggest|largest|major|best (?:customer|client|part)"), "top_parties"),
    (re.compile(r"day ?book|entered|vouchers?|entries"), "day_book"),
    (re.compile(r"expenses?|income (?:break|detail)|p&l detail|where .* money (?:go|went)"), "profit_loss"),
    (re.compile(r"profit|loss|p\s*&\s*l|\bpnl\b|made money|net income|bottom line"), "profit_loss_summary"),
]


def rules_plan(masked_question: str, role: str, period: Period) -> Plan:
    q = masked_question.lower()
    has_party = bool(_tokens(masked_question))
    chosen: str | None = None

    if has_party:
        for pat, rid in _RULES_WITH_PARTY:
            if pat.search(q):
                chosen = rid
                break
        chosen = chosen or "ledger_balance"
    else:
        for pat, rid in _RULES_GENERAL:
            if pat.search(q):
                chosen = rid
                break

    if chosen is None:
        titles = ", ".join(r.title for r in catalogue_for_role(role))
        return Plan(None, source="rules", period=period,
                    clarification=f"I could not match that to a report. I can show: {titles}.")

    spec = REPORTS[chosen]
    if not spec.allowed_for(role):
        raise PlanDenied(f"report '{chosen}' requires role {spec.min_role}")
    plan = finalize(spec, {}, masked_question, period)
    plan.source = "rules"
    return plan
