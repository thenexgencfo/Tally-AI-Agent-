---
name: investigate-wrong-answer
description: Diagnose a wrong, empty, slow or unexpected answer from the Tally AI Agent. Use when a user says the numbers don't match Tally, a party isn't found, a report is empty, an answer says data is stale, or the model "routed" to the wrong report.
---

# Investigate a wrong answer

Work backwards from the audit log to the data. Never guess; reproduce.

## 1. Read what the system did

The audit log is the record (read it as the owner/superuser; the app role can't):

```sql
SELECT ts, user_id, report_id, params, row_count, outcome, error_detail, latency_ms, data_as_of, question_masked
FROM audit.query_log ORDER BY id DESC LIMIT 20;
```

- `outcome`: `ok`, `no_match` (nothing routed), `denied` (role/company), `error`.
- `question_masked` shows exactly what the model saw. `params` shows the report arguments (placeholders, not real names).
- `data_as_of` vs now = sync lag. `GET /healthz` shows sync health and `users_file`.

## 2. Classify

| Symptom | Likely cause | Look at |
|---|---|---|
| "I could not match that to a report" | Routing missed: no keyword rule, model down, or ambiguous | `planner.py` rules; `/healthz` (`llm`); add a rule or improve the report `description` |
| Wrong report chosen | Rule order, or model picked badly | `_RULES_*` order; `rules_plan` vs model; the planner prompt |
| Wrong group (debtors vs creditors) | `_PAYABLE` / `_RECEIVABLE` wording | `planner.py::finalize` |
| "No matching ledger" | Name didn't match exactly (reports use exact, case-insensitive, trimmed names), or the name wasn't masked/resolved | Is it in `tally.mst_ledger`? Did `NameIndex` include it? |
| Report empty with real data | Group hierarchy: ledger's group not under the expected group | `tally.v_ledger_group.group_path` for the ledger (see `tally-domain`) |
| Numbers differ from Tally | Period/date wrong; stale sync; cancelled/optional vouchers; closing stock missing; sign misread; opening bills missing | the steps below |
| Slow (> 5 s) | Stale planner statistics after a big sync, a cold model, or a huge bill list | `EXPLAIN ANALYZE` the function; `ANALYZE`; model keep-alive |
| 503 | Redis/DB down, audit write failed, or `users.json` invalid | `/healthz`, logs |
| "last sync FAILED" / "synced N minutes ago" | Sync problem | `sync-troubleshooting` skill |

## 3. Reproduce at the SQL level

```sql
BEGIN;
SELECT set_config('app.allowed_companies', '1', true);       -- act as a user of company 1 (RLS)
SELECT * FROM tally.fn_trial_balance(1, '2026-03-31');
SELECT * FROM tally.fn_ledger_balance(1, 'Acme Interiors Pvt Ltd', '2026-03-31');
SELECT ledger, group_path FROM tally.v_ledger_group WHERE company_id = 1 AND ledger ILIKE '%acme%';
ROLLBACK;
```

Compare the report function's output with the mirror tables (`tally.trn_accounting`, `trn_bill`, `mst_opening_bill`) and then with Tally itself (Day Book, Trial Balance, Outstandings for the same period). The first place the numbers diverge is the bug.

## 4. Trial balance not balancing

Tally's own books always balance. A difference means the mirror is wrong or incomplete: a voucher on a ledger missing from the master list (shown as `(not in ledger masters)`), an unbalanced voucher in the export, missing opening balances or stock, or a half-loaded sync. Check `tally.sync_state` and the loader log before blaming the reports.

## 5. Fix and lock it in

Write a test that reproduces the wrong answer (`tests/test_reports_db.py` with `co600`/`messy_data`, or `test_planner.py` for routing), fix, run `./scripts/test.sh`. For routing misses, the keyword fallback matters as much as the model: it is what answers when the model is down.
