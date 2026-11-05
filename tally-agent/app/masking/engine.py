"""
Masking engine.

Rule the whole system rests on: the local model never receives a client's
identifiers or party names. It sees stable placeholders such as [PARTY_1] and
[PAN_2]. Real values are substituted back in only at the edges -- when calling
the database, and when rendering the answer to a user whose role permits it.

Two detection strategies, deliberately:
  * patterns  -- PAN, GSTIN, Aadhaar, IFSC, email, phone (see recognizers.py)
  * dictionary -- party names taken from the company's own ledger masters.
    Names are not inferable by regex and statistical NER both misses them and
    invents them. An exact list from the books is precise and auditable.
"""
from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass, field

from .recognizers import Match, find_all
from .vault import TokenVault

TOKEN_RE = re.compile(r"\[([A-Z]+)_(\d+)\]")

# Phones and word processors turn ' into ’ and - into –. Tally stores the plain
# characters, so a name typed on an iPhone ("D’Souza") would not match the
# books and would slip past masking. Normalise before matching.
_LOOKALIKES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u02bc": "'", "\u2032": "'",
    "`": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u3000": " ",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
})
# Control characters (incl. NUL, which PostgreSQL text cannot store), zero-width
# and bidirectional-override characters: never meaningful in a question.
_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b-\u200f\u202a-\u202e"
    "\u2060-\u2064\u2066-\u2069\ufeff]")


def normalize_text(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFC", text).translate(_LOOKALIKES))

# Database columns whose value is sensitive in full, whatever it looks like.
SENSITIVE_COLUMNS: dict[str, str] = {
    "gstn": "GSTIN",
    "it_pan": "PAN",
    "pan": "PAN",
    "email": "EMAIL",
    "phone": "PHONE",
    "mobile": "PHONE",
    "bank_account_number": "ACCOUNT",
    "bank_ifsc": "IFSC",
    "mailing_address": "ADDRESS",
    "mailing_pincode": "PINCODE",
    "bank_account_holder": "PARTY",
}

# Columns holding a party/ledger name. Masked only when the value appears in
# the company's party dictionary -- so "Salaries" and "Design Fees" stay
# readable while "Acme Interiors Pvt Ltd" does not.
NAME_COLUMNS: frozenset[str] = frozenset(
    {"party", "party_name", "ledger", "bank_account_holder", "mailing_name"}
)

# Columns that must never be masked: masking them destroys the answer.
NEVER_MASK: frozenset[str] = frozenset(
    {
        "debit", "credit", "amount", "balance", "closing", "opening",
        "outstanding", "total_amount", "net_profit", "total_income",
        "total_expense", "age_days", "bucket", "bill_count", "row_count",
        "txn_date", "bill_date", "date", "dr_cr", "section", "side",
        "voucher_count", "running_balance", "quantity", "rate",
        "credit_limit", "credit_days", "stock_value", "kind",
    }
)


# First words too generic to stand alone as a party alias.
_ALIAS_STOPWORDS: frozenset[str] = frozenset(
    {
        "shree", "shri", "sri", "new", "the", "india", "indian", "national",
        "global", "united", "super", "royal", "star", "city", "general",
        "modern", "universal", "bharat", "sai", "jai", "raj", "m/s", "ms",
        "messrs", "mr", "mrs", "dr", "sales", "purchase", "cash", "bank",
        "office", "total", "account", "ledger", "party", "balance",
    }
)


_HONORIFIC = re.compile(
    r"^(?:m/s\.?|m/s|messrs\.?|shri|shree|sri|smt\.?|mr\.?|mrs\.?|ms\.?|dr\.?|the)\s+",
    re.IGNORECASE)
_SUFFIX = re.compile(
    r"[\s,]+(?:pvt\.?\s*ltd\.?|private\s+limited|p\.?\s*ltd\.?|ltd\.?|limited|llp|"
    r"(?:&|and)\s+co\.?|co\.)$", re.IGNORECASE)
_INITIALS = re.compile(r"^(?:[a-z]\.?\s+)+", re.IGNORECASE)


def _variants(name: str) -> list[str]:
    """Ways people write a ledger name: without 'M/s', initials or 'Pvt Ltd'."""
    out: list[str] = []
    cur = name.strip()
    for _ in range(3):
        nxt = _SUFFIX.sub("", _INITIALS.sub("", _HONORIFIC.sub("", cur))).strip(" ,.")
        if nxt == cur:
            break
        cur = nxt
        out.append(cur)
    return out


@dataclass(frozen=True)
class NameIndex:
    """Exact names from the books, plus safe variants and one-word aliases.
    Every entry maps to the canonical ledger name and its entity type."""

    pattern: re.Pattern[str]
    _canon: dict[str, tuple[str, str]]

    def canonical(self, matched: str) -> str:
        return self._canon.get(normalize_text(matched).lower(), (matched, "PARTY"))[0]

    def entity(self, matched: str) -> str:
        return self._canon.get(normalize_text(matched).lower(), (matched, "PARTY"))[1]

    @classmethod
    def build(cls, party_names: list[str], other_ledgers: list[str],
              company: str | None = None) -> "NameIndex | None":
        canon: dict[str, tuple[str, str]] = {}
        # Keys are normalised (quotes, spaces, case); values keep the exact
        # ledger name, which is what the database is queried with.
        parties = sorted({n for n in party_names if n and len(n.strip()) >= 3})
        # Stored trimmed: the match covers only the visible name, so surrounding
        # spaces in the ledger name stay where they were when unmasked. SQL
        # lookups ignore surrounding spaces (btrim) so the trimmed name finds it.
        for name in parties:
            canon[normalize_text(name).strip().lower()] = (name.strip(), "PARTY")
        if company and len(company.strip()) >= 3:
            canon[normalize_text(company).strip().lower()] = (company.strip(), "COMPANY")

        other_full = {o.strip().lower() for o in other_ledgers}
        other_words = {
            w.lower() for ledger in other_ledgers for w in re.findall(r"[\w&/.-]+", ledger)
        }

        # Variants ("Sharma Traders" for "M/s Sharma Traders"): only if unambiguous
        # and not the name of some non-party ledger.
        owners: dict[str, set[tuple[str, str]]] = {}
        for full, (name, ent) in list(canon.items()):
            for v in _variants(normalize_text(name)):
                if len(v) >= 5 and v.lower() not in other_full:
                    owners.setdefault(v.lower(), set()).add((name, ent))
        for v, who in owners.items():
            if len(who) == 1 and v not in canon:
                canon[v] = next(iter(who))

        # One-word alias: distinctive first word of the name (after stripping
        # honorifics/initials), unique across parties, absent from every
        # non-party ledger name -- so "Design" never masks "Design Fees".
        first_words: dict[str, set[tuple[str, str]]] = {}
        for name, ent in set(canon.values()):
            norm = normalize_text(name).strip()
            core = (_variants(norm) or [norm])[-1]
            words = core.split()
            if len(words) < 2:
                continue
            first_words.setdefault(words[0].lower(), set()).add((name, ent))
        for word, who in first_words.items():
            if (
                len(who) == 1
                and len(word) >= 4
                and word not in _ALIAS_STOPWORDS
                and word not in other_words
                and word not in canon
            ):
                canon[word] = next(iter(who))

        if not canon:
            return None
        alternation = "|".join(
            re.escape(k) for k in sorted(canon, key=len, reverse=True)
        )
        # Word boundaries stop "Acme" matching inside "Acmeson".
        pattern = re.compile(rf"(?<![\w])(?:{alternation})(?![\w])", re.IGNORECASE)
        return cls(pattern, canon)


class MaskPolicy(enum.StrEnum):
    TOKENIZE = "tokenize"   # reversible placeholder, for the model
    PARTIAL = "partial"     # last 4 kept, irreversible, for low-trust display
    REDACT = "redact"       # removed entirely
    KEEP = "keep"           # passed through


@dataclass
class MaskResult:
    text: str
    matches: list[Match] = field(default_factory=list)

    @property
    def entities(self) -> set[str]:
        return {m.entity for m in self.matches}


# How a placeholder is shown to someone who may not see the real value.
# Identifiers keep their last 4 characters (the usual convention for PAN,
# account and phone numbers). For names and free text, trailing characters are
# both useless and leaky, so they get a stable pseudonym or are withheld.
_PSEUDONYM = {"PARTY": "Party {n}", "COMPANY": "the company", "ADDRESS": "[address withheld]",
              "EMAIL": "[email withheld]", "PINCODE": "[pincode withheld]"}


def masked_display(entity: str, n: str, value: str) -> str:
    if entity in _PSEUDONYM:
        return _PSEUDONYM[entity].format(n=n)
    return partial(value)


def partial(value: str, keep: int = 4) -> str:
    v = str(value)
    if len(v) <= keep:
        return "X" * len(v)
    return "X" * (len(v) - keep) + v[-keep:]


class MaskingEngine:
    def __init__(self, vault: TokenVault) -> None:
        self._vault = vault

    # ---------------------------------------------------------- dictionary
    @staticmethod
    def build_name_index(
        party_names: list[str], other_ledgers: list[str] | None = None
    ) -> "NameIndex | None":
        return NameIndex.build(party_names, other_ledgers or [])

    # ---------------------------------------------------------------- mask
    async def mask_text(
        self,
        text: str,
        session: str,
        *,
        names: "NameIndex | None" = None,
        include_column_only: bool = False,
    ) -> MaskResult:
        if not text:
            return MaskResult(text or "", [])
        text = normalize_text(text)

        matches: list[Match] = list(
            find_all(text, include_column_only=include_column_only)
        )
        if names is not None:
            for m in names.pattern.finditer(text):
                if any(m.start() < x.end and x.start < m.end() for x in matches):
                    continue
                # Store the canonical ledger name, so "Acme" in a question and
                # "Acme Interiors Pvt Ltd" in a result share one token and the
                # database receives the exact name.
                canonical = names.canonical(m.group(0))
                matches.append(Match(m.start(), m.end(), canonical, names.entity(m.group(0))))

        if not matches:
            return MaskResult(text, [])

        matches.sort(key=lambda m: m.start)
        out: list[str] = []
        cursor = 0
        for m in matches:
            token = await self._vault.token_for(session, m.entity, m.value)
            out.append(text[cursor:m.start])
            out.append(token)
            cursor = m.end
        out.append(text[cursor:])
        return MaskResult("".join(out), matches)

    async def mask_rows(
        self,
        rows: list[dict],
        session: str,
        *,
        names: "NameIndex | None" = None,
    ) -> list[dict]:
        """Mask database rows before they are shown to the model."""
        masked: list[dict] = []
        for row in rows:
            new: dict = {}
            for key, value in row.items():
                lkey = key.lower()
                if value is None or value == "":
                    new[key] = value
                elif lkey in NEVER_MASK:
                    new[key] = value
                elif lkey in SENSITIVE_COLUMNS:
                    new[key] = await self._vault.token_for(
                        session, SENSITIVE_COLUMNS[lkey], str(value)
                    )
                elif lkey in NAME_COLUMNS and names is not None:
                    res = await self.mask_text(str(value), session, names=names)
                    new[key] = res.text
                elif isinstance(value, str):
                    # Free text such as a narration: run pattern recognizers and
                    # the party dictionary over it.
                    res = await self.mask_text(value, session, names=names)
                    new[key] = res.text
                else:
                    new[key] = value
            masked.append(new)
        return masked

    async def tokenize(self, session: str, entity: str, value: str) -> str:
        """Placeholder for a value the caller already knows is sensitive."""
        return await self._vault.token_for(session, entity, value)

    # -------------------------------------------------------------- unmask
    async def unmask_text(self, text: str, session: str) -> str:
        """Replace [ENTITY_n] placeholders with their real values."""
        if not text:
            return text
        out: list[str] = []
        cursor = 0
        for m in TOKEN_RE.finditer(text):
            value = await self._vault.value_for(session, m.group(0))
            out.append(text[cursor:m.start()])
            out.append(value if value is not None else m.group(0))
            cursor = m.end()
        out.append(text[cursor:])
        return "".join(out)

    async def redisplay(self, text: str, session: str, *, may_unmask: bool) -> str:
        """Render for a user: real values if their role allows, else partial."""
        if may_unmask:
            return await self.unmask_text(text, session)
        out: list[str] = []
        cursor = 0
        for m in TOKEN_RE.finditer(text):
            value = await self._vault.value_for(session, m.group(0))
            out.append(text[cursor:m.start()])
            out.append(masked_display(m.group(1), m.group(2), value) if value else m.group(0))
            cursor = m.end()
        out.append(text[cursor:])
        return "".join(out)

    async def unmask_params(self, params: dict, session: str) -> dict:
        """Model-supplied parameters carry tokens; the database needs real values."""
        resolved: dict = {}
        for key, value in params.items():
            resolved[key] = (
                await self.unmask_text(value, session)
                if isinstance(value, str)
                else value
            )
        return resolved

    @staticmethod
    def contains_token(text: str) -> bool:
        return bool(TOKEN_RE.search(text or ""))
