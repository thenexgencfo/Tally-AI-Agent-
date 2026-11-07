---
name: add-report
description: Add or change a report in the Tally AI Agent (SQL function, registry entry, answer summary, question routing, tests, docs). Use whenever someone wants the agent to answer a new kind of question, or an existing report's figures or parameters change.
---

# Add or change a report

A report is the ONLY way the model can read data. Adding one is a reviewed code change, never a model decision. Work from `tally-agent/`.

## Steps (all of them, in this order)

1. **Write the SQL function** in `sql/03_reports.sql`.
   - `LANGUAGE sql STABLE SECURITY INVOKER SET search_path = tally, pg_temp`, so it runs under the caller's row-level security.
   - First parameter is `p_company integer`. Every table read is filtered by `company_id = p_company`.
   - Name it `fn_<name>`. Exclude cancelled/optional vouchers the way the others do (`is_cancelled = 0 AND is_optional = 0`).
   - **Signs:** debit negative, credit positive (see `tally-domain`).
   - **Totals must be complete.** Never aggregate over a function that has a row limit. Call sibling functions with `NULL` as the limit.
   - A `limit` parameter is only for rankings ("top N"): `LIMIT LEAST(COALESCE(p_limit, 10), 100)`.
   - Use ordinals in `ORDER BY` (`ORDER BY 2 DESC`), not output-column names.
   - Changing an existing function's signature or return columns? Add a `DROP FUNCTION IF EXISTS` at the top of the file first.
2. **Add the registry entry** in `app/reports/registry.py` (`ReportSpec`): `id`, `title`, a `description` the model will read (say what it returns and what `group`/`limit` mean), `function="tally.fn_<name>"`, typed `params` (`ParamSpec`), `min_role`, `examples`. Parameter types: `date`, `text`, `int`, `numeric`, `ledger`, `group`. Mark row limits that the user must not control `internal=True`. Set `sensitive=True` and `min_role="senior"` if it exposes PAN/GSTIN/bank/contact data. Sensitive reports are never cached.
3. **Add the summary** in `app/agent/answer.py::summarize` (a branch on the report id). Compute every figure with `Decimal` and `format_inr`. Never let the model state a number. If rows can be empty, handle `n == 0`.
4. **Add routing** in `app/agent/planner.py`: a keyword rule in `_RULES_GENERAL` (no party in the question) or `_RULES_WITH_PARTY` (with one), so the system still works with the model down. **Order matters**: put specific patterns before general ones. Add any new parameter parsing to `finalize()`.
5. **Mask anything new.** If the report returns a new column holding names, identifiers or free text, make sure `app/masking/engine.py` handles it (`SENSITIVE_COLUMNS`, `NAME_COLUMNS`, `REF_COLUMNS`) and that a number-like column is in `NEVER_MASK`. See the `change-masking` skill.
6. **Tests** (see `test-and-verify`):
   - `tests/test_reports_db.py`: hand-checked figures against the seeded demo company (and `co600` / `tests/messy_data.py` for messy cases such as advances, opening bills, 250+ rows, several years).
   - `tests/test_planner.py`: the questions that should route to it, with the `group`/`limit` they should produce.
   - `tests/test_security_e2e.py`: if it handles names, add it to `QUESTIONS` so the "nothing reaches the model" test covers it.
7. **Rebuild and run**: `./scripts/test.sh -q -p no:warnings`. All tests must pass.
8. **Docs**: update the report count and list in `README.md`, `tally-agent/README.md` and `CLAUDE.md` ("the 15 reports"), and the report list in `tally-agent/README.md`.

## Check before you finish

- Does a junior see only masked names for it? (`min_role`, masking, `redisplay`)
- Does a user of company A get nothing for company B? (RLS plus the e2e isolation tests)
- Is the answer right when the model is down? (keyword route works)
- Does it stay fast on `tests/messy_data.py` with 6,000+ vouchers?
