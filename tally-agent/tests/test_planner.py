import datetime as dt

import pytest

from app.agent.dates import resolve_period
from app.agent.planner import PlanDenied, Planner
from tests.conftest import FakeLLM

TODAY = dt.date(2025, 3, 31)


@pytest.mark.parametrize("text,expected", [
    ("net profit for FY 2024-25", ("2024-04-01", "2025-03-31")),
    ("fy 2023-2024", ("2023-04-01", "2024-03-31")),
    ("as on 31/03/2025", ("2024-04-01", "2025-03-31")),
    ("from 1 May 2024 to 15 June 2024", ("2024-05-01", "2024-06-15")),
    ("sales in May", ("2024-05-01", "2024-05-31")),
    ("sales in January", ("2025-01-01", "2025-01-31")),
    ("sales in May", ("2024-05-01", "2024-05-31")),     # most recent May, not a future one
    ("last month", ("2025-02-01", "2025-02-28")),
    ("last quarter", ("2024-10-01", "2024-12-31")),
    ("last year", ("2023-04-01", "2024-03-31")),
])
def test_date_resolution(text, expected):
    p = resolve_period(text, TODAY)
    assert (p.date_from.isoformat(), p.date_to.isoformat()) == expected and p.explicit


def test_no_period_defaults_to_fy_to_date():
    p = resolve_period("trial balance", TODAY)
    assert (p.date_from, p.as_of, p.explicit) == (dt.date(2024, 4, 1), TODAY, False)


async def test_model_unknown_report_falls_back_to_rules():
    llm = FakeLLM(plan={"report_id": "drop_all_tables", "params": {}})
    plan = await Planner(llm).plan("receivables ageing", "junior", TODAY)
    assert plan.report_id == "ageing_summary" and plan.source == "rules"
    assert any("keyword routing" in w for w in plan.warnings)


async def test_model_choosing_report_above_role_is_denied():
    llm = FakeLLM(plan={"report_id": "party_details", "params": {"party": "[PARTY_1]"}})
    with pytest.raises(PlanDenied):
        await Planner(llm).plan("gst number of [PARTY_1]", "junior", TODAY)


async def test_model_invented_name_replaced_by_question_placeholder():
    llm = FakeLLM(plan={"report_id": "ledger_balance",
                        "params": {"ledger": "Acme Interiors", "as_of": "2025-03-31"}})
    plan = await Planner(llm).plan("how much does [PARTY_1] owe", "junior", TODAY)
    assert plan.params["ledger"] == "[PARTY_1]" and plan.source == "llm"


async def test_model_name_without_placeholder_in_question_asks_back():
    llm = FakeLLM(plan={"report_id": "ledger_balance", "params": {"ledger": "Somebody Ltd"}})
    plan = await Planner(llm).plan("how much does somebody owe", "junior", TODAY)
    assert plan.report_id is None and "Which party" in plan.clarification


async def test_code_dates_override_model_dates_when_question_is_explicit():
    llm = FakeLLM(plan={"report_id": "profit_loss_summary",
                        "params": {"date_from": "2019-01-01", "date_to": "2019-12-31"}})
    plan = await Planner(llm).plan("net profit for FY 2024-25", "junior", TODAY)
    assert (plan.params["date_from"], plan.params["date_to"]) == ("2024-04-01", "2025-03-31")


async def test_unexpected_model_params_are_dropped():
    llm = FakeLLM(plan={"report_id": "trial_balance", "params": {"as_of": "2025-03-31",
                                                                 "raw_sql": "SELECT 1"}})
    plan = await Planner(llm).plan("trial balance", "junior", TODAY)
    assert "raw_sql" not in plan.params and any("raw_sql" in w for w in plan.warnings)


async def test_model_down_uses_rules():
    plan = await Planner(FakeLLM(fail=True)).plan("cash and bank balance", "junior", TODAY)
    assert plan.report_id == "cash_bank_balance"


async def test_payables_wording_selects_creditors():
    plan = await Planner(None).plan("which supplier bills are overdue", "junior", TODAY)
    assert plan.report_id == "bills_outstanding" and plan.params["group"] == "Sundry Creditors"


async def test_planner_prompt_contains_only_masked_question():
    llm = FakeLLM(plan={"report_id": "none", "params": {}})
    await Planner(llm).plan("balance of [PARTY_1]", "junior", TODAY)
    system, user_msg = llm.prompts[0]
    assert user_msg == "balance of [PARTY_1]"
    assert "party_details" not in system   # junior's catalogue excludes senior-only reports


def test_month_without_year_is_never_in_the_future():
    p = resolve_period("sales in January", dt.date(2026, 11, 5))
    assert p.date_from == dt.date(2026, 1, 1)


def test_unreadable_date_warns():
    p = resolve_period("balance as on 31/02/2025", TODAY)
    assert p.warning and "31/02/2025" in p.warning


@pytest.mark.parametrize("q,group", [
    ("list debtors who still need to pay us", "Sundry Debtors"),
    ("customers yet to pay", "Sundry Debtors"),
    ("bills we need to pay", "Sundry Creditors"),
    ("supplier bills overdue", "Sundry Creditors"),
])
async def test_receivable_vs_payable_routing(q, group):
    plan = await Planner(None).plan(q, "junior", TODAY)
    assert plan.params["group"] == group


async def test_model_receivables_choice_respected_for_customer_questions():
    llm = FakeLLM(plan={"report_id": "bills_outstanding", "params": {"group": "Sundry Debtors"}})
    plan = await Planner(llm).plan("customers yet to pay", "junior", TODAY)
    assert plan.params["group"] == "Sundry Debtors"
