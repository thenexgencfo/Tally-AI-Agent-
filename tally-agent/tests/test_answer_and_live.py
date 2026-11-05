from decimal import Decimal

import httpx
import pytest

from app.agent.answer import format_inr, grounded, summarize
from app.agent.live import LiveUnavailable, TallyLive, build_ledger_request, parse_closing
from app.reports.registry import REPORTS


@pytest.mark.parametrize("v,s", [(0, "₹0.00"), (999, "₹999.00"), (150000, "₹1,50,000.00"),
                                 (12345678.9, "₹1,23,45,678.90"), (-238000, "-₹2,38,000.00")])
def test_inr(v, s):
    assert format_inr(v) == s


def test_summary_is_computed_not_generated():
    rows = [{"bucket": "91-180", "bill_count": 1, "outstanding": "177000.00"},
            {"bucket": "180+", "bill_count": 1, "outstanding": "136000.00"}]
    s = summarize(REPORTS["ageing_summary"], rows, {"group": "Sundry Debtors", "as_of": "2025-03-31"})
    assert "₹3,13,000.00" in s


def test_trial_balance_summary_flags_imbalance():
    rows = [{"debit": "100.00", "credit": "0"}, {"debit": "0", "credit": "90.00"}]
    assert "OUT OF BALANCE" in summarize(REPORTS["trial_balance"], rows, {"as_of": "2025-03-31"})


def test_grounding():
    ev = 'total ₹1,36,000.00 [PARTY_1] {"age_days": 320}'
    assert grounded("[PARTY_1] owes ₹1,36,000.00 and is 320 days old", evidence=ev)[0]
    assert not grounded("[PARTY_1] owes ₹2,00,000.00", evidence=ev)[0]
    assert not grounded("[PARTY_7] owes ₹1,36,000.00", evidence=ev)[0]
    assert grounded("Two of the bills are older than 90 days", evidence=ev)[0]   # small numbers ok


def test_live_request_is_export_only_and_refuses_breakout():
    xml = build_ledger_request("White Design Consultants", "Acme & Co")
    assert "<TALLYREQUEST>Export</TALLYREQUEST>" in xml and "Import" not in xml
    assert "Acme &amp; Co" in xml
    for bad in ['Acme" or 1=1', "x$$Delete", "a\nb"]:
        with pytest.raises(LiveUnavailable):
            build_ledger_request("Co", bad)


def test_live_parse():
    assert parse_closing("<ENVELOPE><LEDGER NAME='A'><CLOSINGBALANCE>-1,50,000.00</CLOSINGBALANCE>"
                         "</LEDGER></ENVELOPE>") == Decimal("-150000.00")
    assert parse_closing("<ENVELOPE></ENVELOPE>") is None


async def test_live_client_against_mock_tally():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, text="<ENVELOPE><LEDGER><CLOSINGBALANCE>-152000.00"
                                        "</CLOSINGBALANCE></LEDGER></ENVELOPE>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    v = await TallyLive("http://tally:9000").ledger_closing("Co", "Acme Interiors Pvt Ltd", client=client)
    assert v == Decimal("-152000.00") and "Acme Interiors Pvt Ltd" in seen["body"]


@pytest.mark.parametrize("text", ["[PARTY_1] owes INR750000.", "[PARTY_1] owes ₹7.5 lakh.",
                                  "[PARTY_1] owes ₹2 crore.", "[PARTY_1] owes 1e6 rupees.",
                                  "[PARTY_1] owes 750k."])
def test_grounding_rejects_scaled_and_glued_figures(text):
    assert not grounded(text, evidence="[PARTY_1] {'closing': '550000.00'}")[0]
