# Security model

This system handles client books for a CA firm. This document lists what it defends against, how each defence is enforced, and the test that proves it. Run `./scripts/test.sh` to check every claim below.

## Threats and controls

| Threat | Control | Enforced in | Proven by |
|---|---|---|---|
| Client identifiers reach the model | Party, loan and capital ledger names (exact names, variants without M/s, initials or Pvt Ltd, and safe one-word aliases), the client company's own name, and PAN, GSTIN (any letter case), Aadhaar (Verhoeff-checked), IFSC, email, phone (any +91/0 format) and account numbers near "a/c"/"bank" are replaced with placeholders before any model call. Rows are masked again before the explanation step | `app/masking/` | `test_no_identifier_or_party_name_ever_reaches_the_model` runs 11 questions through a model that echoes everything, then checks every prompt for 18 secrets |
| Model runs arbitrary queries | No SQL path exists. The model selects a report ID from a fixed registry; values are bound parameters | `app/reports/registry.py` | `test_sql_injection_in_text_param_is_inert`, `test_unknown_and_missing_params_rejected`, `test_model_unknown_report_falls_back_to_rules` |
| Model invents figures | Python computes all numbers. Any explanation containing a figure or placeholder not present in the data, or an amount written with lakh/crore/k or an exponent, is withheld | `app/agent/answer.py` | `test_model_invented_figure_is_withheld`, `test_grounding`, `test_grounding_rejects_scaled_and_glued_figures` |
| Prompt injection via voucher narrations | The planner never sees database content, so a narration can't change which report runs. The explanation step has no tools and is grounding-checked. There is no write path | `app/agent/planner.py`, `app/service.py` | `test_prompt_injection_in_narration_cannot_change_the_report_or_leak`, which uses demo voucher JV-001 carrying an injection payload |
| User reads another company's books | Application check, plus PostgreSQL row-level security driven by a transaction-local setting | `app/service.py`, `sql/04_security.sql` | `test_cross_company_returns_nothing`, `test_cross_company_request_denied_and_audited`, `test_scope_is_transaction_local_and_does_not_leak_to_pool` |
| Junior sees identifiers | Role gate on sensitive reports (`party_details` requires senior). `may_unmask: false` shows "Party 1" and `XXXXXX234K` | registry `min_role`; `engine.redisplay` | `test_junior_cannot_get_party_identifiers`, `test_junior_sees_masked_names_senior_sees_real` |
| Answers leak via Telegram | The bridge principal has `force_masked: true`, so delegated answers are always masked whatever the person's role. The bot replies in private chats only, as plain text | `app/auth.py`; n8n workflow | `test_telegram_bridge_forces_masking_even_for_partner` |
| App role compromise lets someone change data | App role `tally_ro` has SELECT and EXECUTE on report functions only. It can't write to the mirror, can't run ingest, and can't read the audit log | `sql/04_security.sql`, `sql/05_audit.sql` | `test_app_role_cannot_write`, `test_app_role_cannot_ingest`, `test_app_role_cannot_read_audit_log` |
| Audit trail tampered with or skipped | Append-only: statement-level triggers block UPDATE, DELETE **and TRUNCATE**, even for the owner. The audit row is written before data is returned, and if that write fails, nothing is returned | `sql/05_audit.sql`, `AgentService._audit` | `test_audit_log_is_append_only`, `test_audit_log_cannot_be_truncated`, `test_audit_failure_fails_closed` |
| Audit trail becomes a store of personal data | Only the *masked* question, a salted hash and masked parameters are stored | `app/service.py` | `test_audit_trail_holds_no_raw_identifiers` |
| A loader password is used to take over the database | Each company gets its own loader login, confined to its own schema, unable to create functions, with its password kept out of the server log. Ingest locks the loader's tables and reads only plain tables of built-in types, so nothing the loader planted (view, function, custom type, child table) runs with the ingest role's rights | `sql/admin/add_loader_company.sql`, `sql/06_ingest.sql` | `test_ingest_refuses_anything_but_plain_builtin_tables` (3 attacks), `test_app_role_cannot_ingest` |
| Stolen API key | Only SHA-256 hashes are stored, compared in constant time. Per-user rate limits apply. Removing a user from `users.json` revokes access on their next request, with no restart. A broken or mistyped users file refuses everyone (fail closed) rather than keeping the old list | `app/auth.py`, `app/main.py` | `test_requires_key`, `test_rate_limit`, `test_removing_a_user_revokes_access_without_restart` |
| Impersonation through the bridge | Only a `can_delegate` key may send `X-Act-As`, and only for the kinds it is allowed (`tg:` for the Telegram bridge). The request runs with the *target user's* permissions; the bridge's own rate limit applies too; the audit log records both identities | `app/auth.py` | `test_delegation`, `test_bridge_limited_to_its_delegation_kind`, `test_bridge_rate_limit_is_shared_across_impersonated_users` |
| Stale data presented as current | Every answer carries "data as of". Lag over the threshold, or a failed sync, adds a warning. A failed sync never overwrites the last good snapshot | `app/service.py`, `fn_mark_sync_failed` | `test_stale_sync_is_flagged` |
| Network exposure | Caddy is bound to the Tailscale IP only. The agent, model and cache have no internet route (both their networks are internal). Postgres is published on loopback only, for the sync tool | `deploy/docker-compose.yml` | Container test: agent and redis have no default route, outbound connection blocked, 127.0.0.1 database port reachable |
| Malicious content in the UI | Strict CSP (no inline script) and output written with `textContent` only. Frame denial, `no-store`, no API docs exposed | `app/main.py`, `app/static/app.js` | `test_security_headers_and_no_docs`, `test_static_allow_list` |

## Defects found and fixed while building this

These are listed because they are the kind of mistake a review should look for in any similar system:

1. **Views silently bypassed row-level security.** A view owned by a `BYPASSRLS` role runs with its owner's rights, so a user scoped to company 1 could read company 2's trial balance. Fixed with `security_invoker = true`. A deploy-time guard in `04_security.sql` now refuses any view without it.
2. **TRUNCATE could wipe the audit log.** Row-level triggers never fire on TRUNCATE, or on an UPDATE that matches no rows. Fixed with statement-level triggers.
3. **Model output leaked through a warning.** When the grounding check rejected an explanation, the warning quoted the offending value, which put model-generated (possibly injected) text in front of the user. The warning is now generic.
4. **A malformed client IP disabled the service.** An invalid `X-Forwarded-For` value failed the audit insert, and because auditing fails closed, every request was refused. IPs are now validated.
5. **Docker NAT defeats the Caddy IP allow-list.** Caddy sees the bridge gateway, not the tailnet client. The bind address is the real boundary, and the README says so.

## Defects found by the second, independent review, and fixed

Three reviewers examined the code adversarially after the first version. Every finding below was reproduced, fixed, and given a regression test:

1. **Critical:** a loader password allowed full database takeover. A view planted in the loader's schema ran with the ingest role's rights during sync. Fixed by locking and validating source tables and by removing the loader's ability to create functions.
2. **The sync tool could not reach the database at all.** Docker ignores port mappings for containers on internal-only networks. Fixed with a loopback-only `dbhost` network.
3. **Reports came back empty with real Tally data.** The loader records the top-level group ("Current Assets"), not "Sundry Debtors". Fixed by walking the full group hierarchy. The demo data now uses Tally's real structure.
4. **Totals were silently truncated** at 100–200 rows. Fixed: totals always cover every row, and display is capped separately with a warning.
5. **Customer advances were counted as dues, and opening bills were missing.** Fixed: amounts are signed and opening bill allocations are ingested.
6. **Party lookups matched on part of the name,** so they could return another party's GSTIN. Fixed with exact matching.
7. **The trial balance and balance sheet ignored financial-year boundaries and closing stock.** Fixed with a profit carry-forward and stock valuation. Both now balance in every year.
8. **Several identifier formats slipped through masking.** Fixed (see the first row of the table above).
9. **The explanation check accepted "₹7.5 lakh" and "INR750000".** Fixed.
10. **The agent container had an internet route.** Fixed.
11. **The restore drill would have wiped recent audit rows.** It now runs in a throwaway container.
12. **Other issues:** the Windows sync script's exit-code bug, its argument injection, the 3-day task timeout and CRLF line endings; denials not audited; non-finite numbers causing 500s; stale UI results; n8n silent on outage. All fixed.

## Production-condition testing

`tests/test_robustness.py` and `tests/messy_data.py` build a deliberately messy company and ingest it through the real sync path:

- **Names:** Hindi, apostrophes and quotes, regex characters, a 1,000-character name, short names inside longer ones, stray spaces.
- **Structure:** nested and cyclic groups, NULLs, a leap day, four financial years, advances, opening bills, closing stock.
- **Planted faults:** a voucher on a deleted ledger, an unbalanced voucher, a voucher with no date, hostile narrations.

The test then checks:

- every report, for speed;
- that the books balance in every clean year, and that the planted faults are shown and explained;
- no leaks to the model across awkward name spellings, including iPhone curly apostrophes;
- hostile and odd input, such as null bytes, bidirectional tricks, emoji and 1,000-character questions;
- 40 simultaneous questions;
- a hung and a garbage-producing model;
- Redis and database outages, at the HTTP level.

Problems this found, all fixed and kept as tests:

1. **Reports timed out after the first big sync.** Stale planner statistics caused 15 s timeouts on the trial balance and ledger balance. Ingest now runs ANALYZE.
2. **Curly apostrophes leaked names.** "D’Souza" typed on a phone reached the model unmasked. Text is now normalised: quotes, dashes, invisible characters.
3. **A null byte in a question** produced a misleading audit error.
4. **Ledger names with surrounding spaces** could not be found.
5. **A Redis or database outage returned a bare non-JSON 500.** It is now a clear 503 that recovers on its own.
6. **A hung model delayed every question.** A circuit breaker now limits that to one slow question per minute.
7. **Vouchers on deleted ledgers left the trial balance silently out.** Such ledgers are now listed, and the answer explains any difference.

## Where data lives

| Place | Contents | Protection |
|---|---|---|
| Tally | System of record | Unchanged; read only through the XML port |
| PostgreSQL `tally.*` | Mirror of the books | RLS, read-only app role. Disposable: rebuild by full re-sync |
| PostgreSQL `audit.*` | Who asked what, masked | Append-only. **Irreplaceable: this is what backups protect** |
| Redis | Report cache (2 min), placeholder vault (1 h) | Memory only, password, internal network, nothing written to disk |
| Model (Ollama) | Masked text only | Local GPU, no internet route |
| Backups | Full DB and user hashes | `age`-encrypted. Unencrypted backups are refused |
| Telegram | Masked answers | Third party: treat it as outside the firm |

## DPDP Act 2023: how the design maps

This is not legal advice. Have the firm's compliance adviser review it.

- **Purpose limitation and minimisation.** The model receives masked text only. Reports return only the columns they need, and identifiers appear only in a senior-only report.
- **Security safeguards (s.8(5)).** Encryption in transit (TLS) and in backups, least-privilege roles, network isolation, rate limiting.
- **Accountability.** An append-only access log records user, role, report, row count, whether real values were shown, and data freshness.
- **Retention.** Define a period. `audit.fn_purge_older_than(days)` is owner-only and deliberate.
- **Breach readiness.** The audit log answers "who saw what, when". Example query, run as the owner:
  ```sql
  SELECT ts, user_id, report_id, row_count, unmasked_shown
  FROM audit.query_log WHERE ts > now() - interval '7 days' ORDER BY ts DESC;
  ```
- **No cross-border transfer for processing.** All processing is local. Telegram is the exception and only ever receives masked text.

## If a key leaks

1. Delete or disable that user's entry in `config/users.json`. This takes effect on their next request.
2. Review their recent activity with the query above, filtered on `user_id`.
3. Issue a new key with `scripts/new-api-key.ps1`.
