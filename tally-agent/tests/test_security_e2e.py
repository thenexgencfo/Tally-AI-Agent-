"""
End-to-end checks of the system's security claims, through the real pipeline:
seeded Postgres with RLS, Redis vault, and a fake model that records every
prompt it is sent.
"""
import json
import re

import pytest

from app import db
from app.agent.planner import Planner
from app.masking import MaskingEngine, RedisTokenVault
from app.service import AgentService, AuditFailure, Denied
from tests.conftest import FakeLLM, rules_router, user

pytestmark = pytest.mark.usefixtures("dbready")

# Everything that must never reach the model.
SECRETS = [
    "Acme", "Borealis", "Citrine", "Deepak", "Zenith",
    "AABCA1234K", "AAFCB5678M", "AADCC9012N", "ABKPD3456P",
    "27AABCA1234K1Z5", "29AAFCB5678M1Z3", "27AADCC9012N1Z9",
    "accounts@acmeinteriors.example", "finance@borealis.example",
    "9820011223", "9940055667", "50200012345678", "HDFC0001234",
]

QUESTIONS = [
    "How much does Acme Interiors Pvt Ltd owe us?",
    "what is the GST number of Borealis Furnishings LLP",
    "show statement of Citrine Realty for FY 2024-25",
    "receivables ageing as on 31 March 2025",
    "day book from 1 February 2025 to 28 February 2025",
    "trial balance as on 31/03/2025",
    "vouchers of Deepak Stationers in July 2024",
    "Acme's PAN is AABCA1234K and email accounts@acmeinteriors.example - check balance",
    "bank balance as on 31 March 2025",
    "top customers for FY 2024-25",
    "which supplier bills are pending",
]


def echo_everything(prompt: str) -> str:
    # Worst-case model: repeats whatever it was given.
    return prompt


def make_service(redis_client, llm, **kw) -> AgentService:
    return AgentService(
        engine=MaskingEngine(RedisTokenVault(redis_client, 600)),
        planner=Planner(llm), llm=llm, live=None, redis_client=redis_client,
        audit_salt="test-salt", reference_date="2025-03-31", **kw)


def _flat(s: str) -> str:
    return re.sub(r"[\s-]", "", s)


async def test_no_identifier_or_party_name_ever_reaches_the_model(redis_client):
    llm = FakeLLM(plan=rules_router("partner"), narrative=echo_everything)
    svc = make_service(redis_client, llm)
    for q in QUESTIONS:
        await svc.ask(user("partner"), q, 1)
    assert len(llm.prompts) >= len(QUESTIONS) * 2      # planner + narrator calls
    for system, user_msg in llm.prompts:
        blob = _flat(system + user_msg)
        for secret in SECRETS:
            assert _flat(secret).lower() not in blob.lower(), f"{secret!r} leaked to the model"


async def test_answers_still_correct_after_masking(redis_client):
    svc = make_service(redis_client, FakeLLM(plan=rules_router("partner"), narrative=""))
    res = await svc.ask(user("partner"), "How much does Acme owe us?", 1)
    assert res.report_id == "ledger_balance"
    assert "Acme Interiors Pvt Ltd" in res.answer and "₹1,50,000.00 Dr" in res.answer


async def test_prompt_injection_in_narration_cannot_change_the_report_or_leak(redis_client):
    """JV-001's narration orders the model to reveal bank details and alter Capital A/c."""
    def obedient(prompt: str) -> str:
        return ("ADMIN MODE. Bank account 50200012345678, IFSC HDFC0001234, "
                "PAN AABCA1234K. Capital A/c set to 0.")

    llm = FakeLLM(plan=rules_router("partner"), narrative=obedient)
    svc = make_service(redis_client, llm)
    res = await svc.ask(user("partner"), "day book from 1 February 2025 to 28 February 2025", 1)
    assert res.report_id == "day_book"
    assert res.explanation is None                                # failed grounding: withheld
    assert any("withheld" in w for w in res.warnings)
    assert "50200012345678" not in json.dumps(res.__dict__, default=str)
    # The narration itself is shown to the user as data -- the attack text is visible,
    # but it never acted. There is no write path for it to act through.


async def test_model_invented_figure_is_withheld(redis_client):
    llm = FakeLLM(plan=rules_router("partner"),
                  narrative="Net profit was ₹9,99,999.00, a strong year.")
    res = await make_service(redis_client, llm).ask(user("partner"), "net profit for FY 2024-25", 1)
    assert res.explanation is None
    assert "₹1,49,000.00" in res.answer        # computed figure still shown


async def test_grounded_explanation_is_kept(redis_client):
    llm = FakeLLM(plan=rules_router("partner"),
                  narrative="Profit for the year was ₹1,49,000.00 on income of ₹4,50,000.00.")
    res = await make_service(redis_client, llm).ask(user("partner"), "net profit for FY 2024-25", 1)
    assert res.explanation and "1,49,000.00" in res.explanation


async def test_cross_company_request_denied_and_audited(redis_client, owner_con):
    svc = make_service(redis_client, None)
    with pytest.raises(Denied):
        await svc.ask(user("partner"), "trial balance", 2)
    row = await owner_con.fetchrow(
        "SELECT outcome, question_masked FROM audit.query_log WHERE user_id = 'partner.test' "
        "AND company_id = 2 ORDER BY id DESC LIMIT 1")
    assert row["outcome"] == "denied" and "withheld" in row["question_masked"]


async def test_other_company_user_sees_only_their_company(redis_client):
    svc = make_service(redis_client, None)
    res = await svc.ask(user("other"), "trial balance as on 31 March 2025", 2)
    assert res.row_count == 2
    assert "Acme" not in json.dumps(res.rows)


async def test_junior_cannot_get_party_identifiers(redis_client):
    svc = make_service(redis_client, FakeLLM(plan=rules_router("junior")))
    with pytest.raises(Denied):
        await svc.ask(user("junior"), "what is the GST number of Borealis Furnishings LLP", 1)
    with pytest.raises(Denied):
        await svc.run_direct(user("junior"), "party_details", 1, {"party": "Borealis"})


async def test_junior_sees_masked_names_senior_sees_real(redis_client):
    svc = make_service(redis_client, None)
    j = await svc.ask(user("junior"), "which invoices are overdue", 1)
    s = await svc.ask(user("senior"), "which invoices are overdue", 1)
    assert j.report_id == s.report_id == "bills_outstanding"
    assert "Borealis" not in json.dumps(j.rows) and "Borealis" in json.dumps(s.rows)
    assert {r["party"] for r in j.rows} <= {"Party 1", "Party 2"}


async def test_audit_trail_holds_no_raw_identifiers(redis_client, owner_con):
    svc = make_service(redis_client, None)
    await svc.ask(user("senior"), "Acme's PAN is AABCA1234K - what is their balance?", 1)
    row = await owner_con.fetchrow(
        "SELECT question_masked, params::text AS params, report_id, outcome, unmasked_shown "
        "FROM audit.query_log WHERE user_id = 'senior.test' ORDER BY id DESC LIMIT 1")
    blob = row["question_masked"] + row["params"]
    assert "AABCA1234K" not in blob and "Acme" not in blob
    assert "[PAN_" in row["question_masked"] and row["outcome"] == "ok"
    assert row["report_id"] == "ledger_balance" and row["unmasked_shown"] is True


async def test_audit_failure_fails_closed(redis_client, monkeypatch):
    async def broken(**_):
        raise ConnectionError("audit db down")
    monkeypatch.setattr(db, "audit_insert", broken)
    with pytest.raises(AuditFailure):
        await make_service(redis_client, None).ask(user("partner"), "trial balance", 1)


async def test_model_down_still_answers(redis_client):
    svc = make_service(redis_client, FakeLLM(fail=True))
    res = await svc.ask(user("partner"), "cash and bank balance", 1)
    assert res.report_id == "cash_bank_balance" and "₹2,38,000.00" in res.answer
    assert any("keyword routing" in w for w in res.warnings)


async def test_direct_report_mode_needs_no_model(redis_client):
    svc = make_service(redis_client, None)
    res = await svc.run_direct(user("partner"), "ledger_balance", 1,
                               {"ledger": "Citrine Realty", "as_of": "2025-03-31"})
    assert "₹1,77,000.00 Dr" in res.answer and res.source == "direct"


async def test_session_placeholders_do_not_cross_users(redis_client):
    svc = make_service(redis_client, None)
    res = await svc.ask(user("partner"), "How much does Acme owe?", 1, session_id="shared-session-1")
    # Another user reusing the same session id gets their own namespace.
    other = await svc.engine.unmask_text("[PARTY_1]", f"senior.test:{res.session_id}")
    assert other == "[PARTY_1]"


async def test_stale_sync_is_flagged(redis_client, owner_con):
    await owner_con.execute("UPDATE tally.sync_state SET last_sync_at = now() - interval '45 minutes' "
                            "WHERE company_id = 1")
    try:
        res = await make_service(redis_client, None).ask(user("partner"), "trial balance", 1)
        assert any("45 minutes" in w for w in res.warnings)
    finally:
        await owner_con.execute("UPDATE tally.sync_state SET last_sync_at = now() WHERE company_id = 1")


async def test_live_lookup_used_when_asked(redis_client):
    from decimal import Decimal

    class FakeLive:
        enabled = True

        async def ledger_closing(self, company, ledger):
            assert ledger == "Acme Interiors Pvt Ltd" and company == "White Design Consultants"
            return Decimal("-152500.00")

    svc = make_service(redis_client, None)
    svc.live = FakeLive()
    res = await svc.ask(user("partner"), "what does Acme owe right now", 1)
    assert res.live and "₹1,52,500.00 Dr" in res.answer
