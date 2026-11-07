# Tally AI Agent: guide for Claude Code

Staff at a CA firm ask questions in plain English and get answers from TallyPrime data, using a **local** AI model. Everything runs on the firm's Windows PC. This file is the project knowledge; the `.claude/skills/` folder holds step-by-step procedures. Read the matching skill before doing that kind of work.

## The five rules. Never break these.

1. **The model never sees client identifiers.** PAN, GSTIN, Aadhaar, phone, email, bank account numbers, party names and the client company's own name are replaced with placeholders (`[PARTY_1]`) before ANY text reaches the model. Every prompt is built from masked text only.
2. **The model never writes SQL.** It picks a `report_id` from the fixed registry (`tally-agent/app/reports/registry.py`) and fills typed parameters. Parameters are bound, never interpolated.
3. **The model never produces a number.** Python computes every figure (`app/agent/answer.py`, `Decimal`). A model explanation is dropped unless every figure and placeholder in it appears in the data (`grounded()`).
4. **Audit before answer, fail closed.** Every request writes `audit.query_log` (masked question only) before data is returned. If the write fails, nothing is returned. The table is append-only (UPDATE, DELETE, TRUNCATE blocked).
5. **Read-only everywhere.** The app DB role `tally_ro` can only SELECT and call report functions. The Tally live client sends Export requests only. Nothing writes to Tally.

If a change would weaken any of these, stop and tell the user instead of doing it.

## Layout

```
README.md, CLAUDE.md          this guide and the public overview
.claude/skills/               procedures (see the list at the bottom)
tally-agent/
  app/main.py                 FastAPI routes, auth dependency, headers, 503 handling
  app/service.py              THE ask pipeline (AgentService.ask / run_direct / _execute)
  app/auth.py                 users.json schema, key hashes, delegation, rate limit
  app/db.py                   company_scope() (row-level security scope), audit_insert
  app/masking/                recognizers.py (patterns), engine.py (NameIndex, mask/unmask), vault.py (Redis)
  app/agent/                  planner.py (model + keyword fallback), dates.py (Indian FY), answer.py (summaries, grounding), llm.py (Ollama + circuit breaker), live.py (Tally XML export)
  app/reports/registry.py     the 15 reports: the security boundary
  app/static/                 staff web page (no inline script, textContent only)
  sql/01..06, 99              schema, sync meta, report functions, RLS, audit, ingest, demo seed
  sql/admin/                  per-company loader login provisioning
  deploy/                     docker-compose.yml, Caddyfile, .env.example, postgres init
  sync/, scripts/             Windows sync runner, company setup, API keys, backup/restore (PowerShell)
  n8n/                        Telegram bot and health-alert workflows (JSON)
  tests/                      pytest suite (needs local PostgreSQL + Redis)
  docs/diagrams/              architecture diagrams
FORMS-SETUP.md, audit-common.js, form-tests/   leftovers from the store-audit forms. The two HTML pages were removed from this repo. Do not maintain these unless the user asks.
```

## Commands (run from `tally-agent/`)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'     # once
./scripts/test.sh -q -p no:warnings        # DROPS and rebuilds the local `tally` DB with demo data, then runs all tests
./.venv/bin/python -m pytest -q tests/test_masking.py            # one file (after test.sh has built the DB)
```

- Needs PostgreSQL 16 and Redis on localhost, `sudo -u postgres psql` access, Python 3.11+. DB-dependent tests skip if Postgres is unreachable, so **a green run with skips proves little**. Check the "passed" count (about 167).
- Test credentials are throwaway: `tally_ro/devro`, `tally_owner/devowner` (needs `ALLOW_WEAK_PASSWORDS=1`; `test.sh` sets it). Tests use Redis DB 15 and set their own env in `tests/conftest.py`.
- After editing `sql/*.sql`, the simplest correct step is to re-run `./scripts/test.sh`. It rebuilds from scratch, so ownership and grants are right.
- Run the app: `REFERENCE_DATE=2025-03-31 USERS_FILE=config/users.json LLM_ENABLED=false ./.venv/bin/uvicorn app.main:app --port 8080`. Make `config/users.json` from `config/users.example.json` and `scripts/new_api_key.py`. The demo company's books end 31 Mar 2025, so pin "today" with `REFERENCE_DATE`.
- Production stack: `cd deploy && docker compose --env-file .env up -d --build` (see `SETUP-WINDOWS.md`).

## Domain facts you must keep straight

- **Tally sign convention (kept everywhere):** DEBIT is NEGATIVE, CREDIT is POSITIVE in `amount` and `opening_balance`. Display: `debit = GREATEST(-x, 0)`, `credit = GREATEST(x, 0)`. A correct trial balance sums to zero.
- Full Tally knowledge (groups, bills, credit days, financial year, loader quirks): `.claude/skills/tally-domain/SKILL.md`.
- Indian financial year runs 1 April to 31 March. Amounts display in Indian grouping (`₹12,34,567.89`, `format_inr`).
- Data flow: Tally XML :9000 → tally-database-loader (Node, external project by dhananjay1405) → schema `loader_c<id>` → `tally.fn_ingest()` (validates, replaces the company's snapshot in one transaction) → `tally.*` mirror (RLS per company) → report functions → agent.

## Gotchas that have already cost time

- **Views must be `security_invoker`.** A view owned by a BYPASSRLS role silently skips row-level security. `04_security.sql` has a guard that fails the deploy.
- **New table?** Add it to the RLS list in `04_security.sql` and to the delete list and copy in `06_ingest.sql`.
- **Changing a report function's signature or return columns** needs `DROP FUNCTION` first (see the top of `03_reports.sql`). `CREATE OR REPLACE` cannot change them.
- **`ORDER BY` an output column name** inside a `RETURNS TABLE` function can resolve to the wrong thing. Use ordinals (`ORDER BY 2 DESC`).
- **Row limits:** a report's `limit` is an internal parameter (`internal=True`, NULL = all rows) so totals are never computed from a truncated list. Only rankings ("top N") expose `limit`.
- **`fn_ingest` reads loader-owned tables.** It locks them and refuses anything but plain tables of built-in types. Never loosen that: a loader password must not become database takeover.
- **Masking names:** the name index is built from the company's own ledgers (`service.company_context`) and rebuilt when the sync time changes. Short all-caps names (ITC) and variants without "M/s"/"Pvt Ltd" are included. Test any new data shape against the leak test.
- **`tests/conftest.py::rules_router`** hard-codes today as 2025-03-31. Don't use it for tests with other dates.
- **Windows PowerShell 5.1** is the target. It has no `RandomNumberGenerator.Fill`, doesn't escape quotes in native-command arguments (we send base64), and `Start-Process -PassThru` needs `$p.Handle` touched before `ExitCode` works. `.ps1` files are stored CRLF, `.sh`/`.sql` LF (`.gitattributes`).
- **Docker networks:** `backend` and `frontend` are `internal: true` (no internet). Postgres's loopback publish only works because it also joins `dbhost`. Caddy needs `AGENT_HOSTNAME` = the Tailscale IP.
- **`users.json` hot-reloads.** An invalid file refuses everyone (503). It never falls back to the old list.

## What is verified, and what is not

Verified: 167 automated tests against real PostgreSQL and Redis; the container stack ran end to end; messy-data, outage, hostile-input and concurrency tests (`tests/test_robustness.py`).

NOT verified, so never claim them:
1. The real Qwen3 model on the GPU. Tests use a stand-in model.
2. Real Tally exports. Demo and messy data follow the loader's published table layout, not a real company.
3. The PowerShell scripts (`sync/`, `scripts/*.ps1`) were never run on Windows.
4. The n8n workflows were never imported into n8n.
5. Live Tally lookups (`TALLY_LIVE_URL`) were never run against real Tally.

## Working agreements

- **Test first for bugs.** Reproduce the failure as a test, fix, show the test passing. Security-relevant changes get a test that tries the attack.
- **Keep the docs true.** If you change a count, path, command or behaviour, update `README.md`, `tally-agent/README.md`, `SECURITY.md`, `SETUP-WINDOWS.md` and `CLAUDE.md` where they mention it.
- **Never commit secrets.** `deploy/.env`, `config/users.json`, `sync/companies.json`, `loader/`, `backups/` are gitignored. API keys are shown once and only their SHA-256 is stored.
- **Git:** work on a feature branch and open a pull request into `master`. Do not push to `master`, and do not force-push. End commit messages with a short explanation of WHY.
- **Be honest in reports.** Say what you ran and what you could not run.

## Skills (in `.claude/skills/`)

| Skill | Use when |
|---|---|
| `add-report` | adding or changing a report (SQL function + registry + summary + routing + tests) |
| `change-masking` | touching recognizers, name matching, placeholders or what the model is shown |
| `test-and-verify` | running tests, writing messy-data tests, container smoke test, proving a change |
| `security-review` | reviewing a change against the five rules, or auditing the repo |
| `investigate-wrong-answer` | a user reports a wrong, empty or slow answer |
| `sync-troubleshooting` | Tally → loader → ingest problems, stale data, failed syncs |
| `windows-ops` | installing, updating, backing up or restoring on the Windows PC |
| `tally-domain` | Tally accounting concepts and loader table quirks |
