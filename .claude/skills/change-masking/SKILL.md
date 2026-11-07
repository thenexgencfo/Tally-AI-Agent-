---
name: change-masking
description: Change how the Tally AI Agent masks client data before the AI model sees it: recognizers, party-name matching, placeholders, role-based display, what rows contain. Use for any leak, false-positive masking, new identifier format, or change to what text goes to the model.
---

# Change masking

Masking is the core privacy control. The goal: the model sees placeholders only (`[PARTY_1]`, `[PAN_1]`), real values come back only for roles allowed to see them. Files in `tally-agent/app/masking/`.

## How it works (read before editing)

- `recognizers.py`: regex recognizers for GSTIN (priority 100, beats PAN because it contains one), PAN, Aadhaar (Verhoeff checksum, so 12-digit amounts aren't masked), IFSC, email, UPI, bank account (only near the words a/c, account, bank), Indian mobile (any `+91`/`0` format). Overlaps are resolved by priority, then length.
- `engine.py`:
  - `normalize_text()`: curly quotes, dashes, NBSP, control and zero-width characters are normalised first ("D’Souza" from an iPhone must match "D'Souza").
  - `NameIndex.build(party_names, other_ledgers, company=)`: exact ledger names, variants without `M/s`, `Shri`, initials, `Pvt Ltd`/`LLP`, one-word aliases (4+ letters, or 3+ if ALL CAPS like `ITC`), and the client company name (`COMPANY` entity). An alias is skipped if it is a stopword, shared by two parties, or appears in a non-party ledger name (so "Design" never masks "Design Fees").
  - `mask_text` / `mask_rows` / `unmask_text` / `redisplay`. Column rules: `NEVER_MASK` (amounts, dates, counts), `SENSITIVE_COLUMNS` (whole value is an identifier), `NAME_COLUMNS` (masked only if in the name index), `REF_COLUMNS` (bill/voucher numbers: party part masked, original text restored verbatim). Any other string column is scanned as free text.
  - Display: a partner sees real values; a junior or the Telegram bridge sees `Party 1`, `XXXXXX234K`, `[email withheld]`, `the company` (`masked_display`).
- `vault.py`: Redis maps token ↔ real value per session (`user:session`), TTL 1 hour, memory only. Sessions are namespaced by user, so guessing a session id gets nothing.
- `app/service.py::company_context` builds the name index per company (parties = ledgers under `PARTY_GROUPS` at any depth, plus voucher parties) and rebuilds it when the sync time changes.

## Rules for changes

- **Fail toward masking.** A false positive (over-masking) is an inconvenience. A false negative is a client-data leak.
- Don't make patterns narrower to "fix" a false positive without a test for the leak case.
- Anything that builds text for the model (`planner.py` prompt, `answer.py::narrative_prompt`) must use masked inputs only. Never put raw question text, raw rows, or exception messages into a prompt, warning, or log.
- Audit rows store the **masked** question only.
- Warnings shown to users must not quote model output (it may be injected text).

## Adding a recognizer

1. Add a `Recognizer(...)` to `RECOGNIZERS` with a priority that resolves overlaps correctly (give validators for ambiguous digit patterns).
2. Add a case to `tests/test_recognizers.py` (positive) and a negative case showing ordinary text, amounts, dates and invoice numbers stay unmasked (`test_amounts_and_invoice_numbers_are_not_masked`).
3. Add the leak string to `SECRETS` in `tests/test_security_e2e.py` and `LEAK_STRINGS` in `tests/test_robustness.py`.
4. If it has a role-specific display form, add it to `masked_display` / `_PSEUDONYM`.

## Proving a masking change

```bash
./scripts/test.sh -q -p no:warnings
./.venv/bin/python -m pytest -q tests/test_security_e2e.py::test_no_identifier_or_party_name_ever_reaches_the_model tests/test_robustness.py::test_awkward_names_resolve_and_never_reach_the_model
```

Both run questions through a **fake model that records every prompt and echoes its input** and then search every prompt for every secret. If you add data shapes, add them to that secret list so the check covers them.
