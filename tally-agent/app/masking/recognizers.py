"""
Pattern recognizers for Indian financial and personal identifiers.

Ordering matters. A GSTIN *contains* a PAN (2 state digits + PAN + 3 chars),
so GSTIN must win over PAN on overlapping spans or every GSTIN would be
shredded into a PAN plus noise. Overlaps are resolved by priority, then by
match length.

Aadhaar numbers are validated with the Verhoeff checksum rather than matched
as "any 12 digits" -- accounting text is full of 12-digit amounts and invoice
numbers, and masking those would destroy the figures we need.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

# ---------------------------------------------------------------- Verhoeff
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    """True if `number` (digits only) passes the Verhoeff checksum."""
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) != 12:
        return False
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][d]]
    return c == 0


def _aadhaar_valid(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    # Aadhaar never starts with 0 or 1.
    return digits[:1] not in ("0", "1") and verhoeff_valid(digits)


# ------------------------------------------------------------------- model
@dataclass(frozen=True)
class Match:
    start: int
    end: int
    value: str
    entity: str


@dataclass(frozen=True)
class Recognizer:
    entity: str
    pattern: re.Pattern[str]
    priority: int
    validator: Callable[[str], bool] | None = None
    # Free-text recognizers run on narrations and user questions. Those marked
    # column_only are too false-positive-prone for free text and only run when
    # the value comes from a known-sensitive database column.
    column_only: bool = False


RECOGNIZERS: list[Recognizer] = [
    Recognizer(
        "GSTIN",
        re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"),
        priority=100,
    ),
    Recognizer(
        "PAN",
        re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
        priority=90,
    ),
    Recognizer(
        "AADHAAR",
        re.compile(r"\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b"),
        priority=88,
        validator=_aadhaar_valid,
    ),
    Recognizer(
        "IFSC",
        re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"),
        priority=85,
    ),
    Recognizer(
        "EMAIL",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        priority=80,
    ),
    Recognizer(
        "UPI",
        re.compile(r"\b[A-Za-z0-9._\-]{3,}@(?:ok[a-z]+|[a-z]{3,10})\b"),
        priority=70,
    ),
    Recognizer(
        "PHONE",
        re.compile(r"(?:\+91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b"),
        priority=60,
    ),
    Recognizer(
        "ACCOUNT",
        re.compile(r"\b\d{9,18}\b"),
        priority=40,
        column_only=True,
    ),
]


def find_all(text: str, *, include_column_only: bool = False) -> list[Match]:
    """Return non-overlapping matches, highest priority first."""
    if not text:
        return []
    candidates: list[tuple[int, int, Match]] = []
    for rec in RECOGNIZERS:
        if rec.column_only and not include_column_only:
            continue
        for m in rec.pattern.finditer(text):
            value = m.group(0)
            if rec.validator and not rec.validator(value):
                continue
            candidates.append(
                (rec.priority, len(value), Match(m.start(), m.end(), value, rec.entity))
            )

    # Highest priority wins; on a tie the longer match wins.
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2].start))
    accepted: list[Match] = []
    for _, _, cand in candidates:
        if any(cand.start < a.end and a.start < cand.end for a in accepted):
            continue
        accepted.append(cand)
    accepted.sort(key=lambda m: m.start)
    return accepted


def entities_in(text: str, *, include_column_only: bool = False) -> set[str]:
    return {m.entity for m in find_all(text, include_column_only=include_column_only)}


def iter_entities() -> Iterable[str]:
    return (r.entity for r in RECOGNIZERS)
