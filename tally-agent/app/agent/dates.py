"""
Deterministic date resolution.

Small local models are unreliable with dates ("last quarter" in an Indian
financial year, "FY 24-25", "as on 31/3"). Dates are therefore resolved by
code from the question; the model's dates are used only when this resolver
finds nothing explicit. Indian FY: 1 April to 31 March.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

from dateutil import parser as dparser

MONTHS = {
    m.lower(): i
    for i, m in enumerate(calendar.month_name)
    if m
} | {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS["sept"] = 9


def fy_bounds(d: dt.date) -> tuple[dt.date, dt.date]:
    start_year = d.year if d.month >= 4 else d.year - 1
    return dt.date(start_year, 4, 1), dt.date(start_year + 1, 3, 31)


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])


def quarter_bounds(d: dt.date) -> tuple[dt.date, dt.date]:
    q_start_month = ((d.month - 1) // 3) * 3 + 1
    start = dt.date(d.year, q_start_month, 1)
    end_month = q_start_month + 2
    return start, dt.date(d.year, end_month, calendar.monthrange(d.year, end_month)[1])


@dataclass(frozen=True)
class Period:
    date_from: dt.date
    date_to: dt.date
    as_of: dt.date
    explicit: bool          # True if the question named a period
    label: str


_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-]+(" + "|".join(sorted(MONTHS, key=len, reverse=True))
    + r")[a-z]*[\s,\-]*(\d{4}|\d{2})?\b"
)
_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")[a-z]*[\s,\-']+(\d{4})\b"
)
_IN_MONTH = re.compile(
    r"\b(?:in|for|during|of)\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b"
)
_FY = re.compile(r"\b(?:fy|f\.y\.|financial year|year)?\s*(20\d{2})\s*[-/–]\s*(\d{2}|20\d{2})\b")


def _explicit_dates(text: str) -> list[dt.date]:
    found: list[tuple[int, dt.date]] = []
    for m in _ISO.finditer(text):
        try:
            found.append((m.start(), dt.date(int(m[1]), int(m[2]), int(m[3]))))
        except ValueError:
            pass
    for m in _DMY.finditer(text):
        try:
            found.append((m.start(), dparser.parse(m[0], dayfirst=True).date()))
        except (ValueError, OverflowError):
            pass
    for m in _DAY_MONTH.finditer(text):
        if not m[3]:
            continue
        try:
            found.append((m.start(), dparser.parse(m[0], dayfirst=True).date()))
        except (ValueError, OverflowError):
            pass
    found.sort(key=lambda x: x[0])
    seen: list[dt.date] = []
    for _, d in found:
        if d not in seen:
            seen.append(d)
    return seen


def resolve_period(text: str, today: dt.date) -> Period:
    t = (text or "").lower()
    fy_from, fy_to = fy_bounds(today)

    m = _FY.search(t)
    if m:
        y1 = int(m[1])
        y2 = int(m[2]) if len(m[2]) == 4 else (y1 // 100) * 100 + int(m[2])
        if y2 == y1 + 1:
            s, e = dt.date(y1, 4, 1), dt.date(y2, 3, 31)
            return Period(s, e, e, True, f"FY {y1}-{str(y2)[2:]}")

    dates = _explicit_dates(t)
    if len(dates) >= 2:
        a, b = sorted(dates[:2])
        return Period(a, b, b, True, f"{a.isoformat()} to {b.isoformat()}")
    if len(dates) == 1:
        d = dates[0]
        s, _ = fy_bounds(d)
        return Period(s, d, d, True, f"as on {d.isoformat()}")

    if re.search(r"\b(last|previous|prior)\s+(fy|financial year|year)\b", t):
        s, e = fy_bounds(fy_from - dt.timedelta(days=1))
        return Period(s, e, e, True, f"FY {s.year}-{str(e.year)[2:]}")
    if re.search(r"\byesterday\b", t):
        d = today - dt.timedelta(days=1)
        return Period(d, d, d, True, "yesterday")
    if re.search(r"\btoday\b", t):
        return Period(today, today, today, True, "today")
    if re.search(r"\b(last|previous)\s+month\b", t):
        first_this = today.replace(day=1)
        s, e = month_bounds((first_this - dt.timedelta(days=1)).year,
                            (first_this - dt.timedelta(days=1)).month)
        return Period(s, e, e, True, s.strftime("%B %Y"))
    if re.search(r"\bthis\s+month\b", t):
        return Period(today.replace(day=1), today, today, True, today.strftime("%B %Y"))
    if re.search(r"\b(last|previous)\s+quarter\b", t):
        s0, _ = quarter_bounds(today)
        s, e = quarter_bounds(s0 - dt.timedelta(days=1))
        return Period(s, e, e, True, f"quarter {s.isoformat()} to {e.isoformat()}")
    if re.search(r"\bthis\s+quarter\b", t):
        s, _ = quarter_bounds(today)
        return Period(s, today, today, True, "this quarter")

    m = _MONTH_YEAR.search(t)
    if m:
        s, e = month_bounds(int(m[2]), MONTHS[m[1]])
        return Period(s, e, e, True, s.strftime("%B %Y"))
    m = _IN_MONTH.search(t)
    if m:
        month = MONTHS[m[1]]
        year = fy_from.year if month >= 4 else fy_from.year + 1
        s, e = month_bounds(year, month)
        return Period(s, e, e, True, s.strftime("%B %Y"))

    if re.search(r"\b(this|current)\s+(fy|financial year|year)\b|\bytd\b|year to date", t):
        return Period(fy_from, today, today, True, f"FY {fy_from.year}-{str(fy_to.year)[2:]} to date")

    return Period(fy_from, today, today, False,
                  f"FY {fy_from.year}-{str(fy_to.year)[2:]} to date")
