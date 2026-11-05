"""
Read-only live lookup against Tally's XML server (port 9000).

Used only for "right now" ledger balances, when the 1-2 minute sync lag
matters. The request type is hard-coded to Export: this client cannot post,
alter or delete anything in Tally.

Verify against your Tally release before enabling (TALLY_LIVE_URL). If it
fails for any reason the answer falls back to the synced mirror.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

import httpx


class LiveUnavailable(Exception):
    pass


def _check_name(value: str) -> str:
    # The name is embedded in a TDL formula string. Refuse anything that could
    # break out of that string rather than trying to escape TDL syntax.
    if not value or any(c in value for c in '"\r\n\x00$|'):
        raise LiveUnavailable("name contains characters not allowed in a live lookup")
    return escape(value)


def build_ledger_request(company: str, ledger: str) -> str:
    company_x = _check_name(company)
    ledger_x = _check_name(ledger)
    return f"""<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE><ID>AgentLedgerBalance</ID></HEADER>
 <BODY><DESC>
  <STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVCURRENTCOMPANY>{company_x}</SVCURRENTCOMPANY>
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="AgentLedgerBalance" ISMODIFY="No">
    <TYPE>Ledger</TYPE>
    <FETCH>Name, ClosingBalance</FETCH>
    <FILTER>AgentByName</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="AgentByName">$Name = "{ledger_x}"</SYSTEM>
  </TDLMESSAGE></TDL>
 </DESC></BODY>
</ENVELOPE>"""


def parse_closing(xml_text: str) -> Decimal | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LiveUnavailable("unparseable response from Tally") from exc
    node = root.find(".//LEDGER/CLOSINGBALANCE")
    if node is None or node.text is None:
        return None
    try:
        # Tally convention, same as the mirror: negative = debit.
        return Decimal(node.text.strip().replace(",", ""))
    except InvalidOperation as exc:
        raise LiveUnavailable("closing balance not numeric") from exc


class TallyLive:
    def __init__(self, url: str, timeout: float = 10) -> None:
        self._url = url
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def ledger_closing(self, company: str, ledger: str,
                             client: httpx.AsyncClient | None = None) -> Decimal | None:
        body = build_ledger_request(company, ledger)
        own = client is None
        client = client or httpx.AsyncClient(timeout=self._timeout)
        try:
            r = await client.post(self._url, content=body.encode("utf-8"),
                                  headers={"Content-Type": "text/xml; charset=utf-8"})
            r.raise_for_status()
            return parse_closing(r.text)
        except httpx.HTTPError as exc:
            raise LiveUnavailable(str(exc)) from exc
        finally:
            if own:
                await client.aclose()
