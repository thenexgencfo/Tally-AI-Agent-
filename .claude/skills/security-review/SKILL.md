---
name: security-review
description: Review a change, or audit the repository, against the Tally AI Agent's security design (model never sees identifiers, no free-form SQL, no model-produced numbers, audit before answer, read-only, per-company isolation). Use before merging anything that touches auth, masking, SQL, ingest, deploy or scripts.
---

# Security review

The system holds client financial data for a CA firm (India, DPDP Act 2023). `tally-agent/SECURITY.md` lists the threats, controls and the test for each. Review against it; keep it current.

## Checklist: ask of every change

1. **Does any new text reach the model?** Trace it. It must come from the masked question, the report catalogue, or masked rows. Never raw questions, raw rows, exception text, narrations or names. Narrations and party names are **untrusted input** (prompt injection): they may only appear inside the `<data>` block of the narrator prompt, and the narrator has no tools.
2. **Does it add a way to run SQL or a function that isn't in `REPORTS`?** It must not. Check for f-strings or `%` building SQL; only registry function names and `int()` values may be interpolated.
3. **Can a number reach the user that Python didn't compute?** Model text must pass `grounded()`. Check the scaled-amount and glued-digit cases (`INR750000`, `₹7.5 lakh`, `1e6`).
4. **Is the audit row written before data leaves, and does failure refuse the request?** Does the row hold only the masked question and masked params?
5. **Company isolation:** is every read inside `db.company_scope([...])`? Do new tables have RLS (`04_security.sql`) and `company_id`? Are new views `security_invoker`? Are cache keys and the vault namespaced by company/user?
6. **Privilege:** does `tally_ro` gain anything beyond SELECT and EXECUTE on report functions plus INSERT on the audit table? Can it still not run `fn_ingest` or read the audit log?
7. **Ingest:** does `fn_ingest` still lock and validate every source table (plain table, built-in column types, no inheritance, no row security) before reading? Loader logins must stay unable to create functions.
8. **Auth:** keys are stored as SHA-256 only and compared in constant time; unknown fields in `users.json` are errors; an invalid file refuses everyone; delegation (`X-Act-As`) is limited to the principal's `delegate_via` kinds; the Telegram bridge keeps `force_masked: true`.
9. **Web page:** no `innerHTML`, no inline script (strict CSP), server data written with `textContent` only.
10. **Network:** Caddy bound to the Tailscale IP only; agent/model/cache have no internet route; Postgres published on 127.0.0.1 only; Tally's port 9000 firewalled from the LAN (it has no password).
11. **Secrets:** nothing in git (`.gitignore`), nothing in logs (the loader password must not appear in the Postgres log: `add_loader_company.sql` sets `log_statement = 'none'`), backups encrypted (`age`), unencrypted backups refused.
12. **Windows scripts:** no raw text into native-command arguments (base64 or stdin), no `Invoke-Expression`, plaintext temp files removed in `finally`.

## Quick greps

```bash
grep -rn "innerHTML\|eval(\|document.write" tally-agent/app/static           # expect nothing
grep -rn "f\"SELECT\|f'SELECT\|% (" tally-agent/app                          # review each hit
grep -rn "ALLOW_WEAK\|devro\|devowner" tally-agent --include=*.sh --include=*.yml --include=*.md   # dev-only, never in deploy/
git grep -n "password\|secret\|api_key" -- ':!*.md' ':!tests' | head -40      # look for committed secrets
```

## Known residual risks (don't claim otherwise)

The real model, real Tally exports, the PowerShell scripts, n8n workflows and live Tally lookups are untested. The loader is a third-party project: pin a commit and review its code before use. Telegram is a third party: only masked text is sent there. The Postgres data directory is not encrypted by the app: recommend BitLocker on the PC.

## Output of a review

List findings by severity with: file and line, a concrete failure scenario, evidence (a command and its output, or "argued from code"), and a minimal fix. Mark each CONFIRMED (reproduced) or PLAUSIBLE. Drop anything you can't substantiate.
