---
name: sync-troubleshooting
description: Debug the Tally to PostgreSQL sync of the Tally AI Agent: tally-database-loader, the loader_c<N> schemas, tally.fn_ingest, sync_state, stale data, failed runs, slow first queries, ingest refusing a schema. Use when answers are stale, empty, or the sync reports FAILED.
---

# Sync troubleshooting

Pipeline (about every 60 s): `sync/run-sync.ps1` (Windows scheduled task) → `loader\c<N>\run.bat` (tally-database-loader, Node) → exports from Tally XML :9000 into schema `loader_c<N>` as login `loader_c<N>` → `SELECT tally.fn_ingest(<N>, 'loader_c<N>', '<company name>', fy_from, fy_to)` as `tally_owner` → mirror `tally.*` + `tally.sync_state`.

## Where to look

| What | Where |
|---|---|
| Sync runner log | `sync\logs\sync-YYYYMMDD.log` (one line per company per run: ok + rows + seconds, or FAILED + reason) |
| Loader logs | `loader\c<N>\import-log.txt`, `error-log.txt` |
| Freshness | `SELECT * FROM tally.v_sync_freshness;` and `GET /healthz` (`sync.failed`, `sync.max_lag_minutes`) |
| Failure reason | `SELECT company_id, last_status, last_error, last_sync_at FROM tally.sync_state;` |

A failed run leaves the **previous good snapshot** in place (and its timestamp), so answers warn "last sync FAILED" or "synced N minutes ago" but stay correct as of the last success.

## Common failures

| Symptom | Cause and fix |
|---|---|
| `loader table loader_cN.<t> not found` | The loader hasn't created its tables yet (first run), the company name in `loader\c<N>\config.json` doesn't match Tally exactly, or `database.schema`/login points elsewhere. Run `run.bat` by hand and read `error-log.txt`. |
| `refusing to ingest ... not plain tables` / `non built-in column types` | `fn_ingest` safety check. Something other than a plain table of built-in types is in the loader schema (a view, domain, child table). Legitimate loader output never does this. **Investigate who created it before loosening anything.** |
| `permission denied for language sql` for a loader login | Intended: loader logins can't create functions. |
| Loader can't connect to Postgres | Postgres is published on `127.0.0.1:5432` only via the `dbhost` network in `deploy/docker-compose.yml`. Check the container, `docker compose ps`, and that `loader\c<N>\config.json` uses `server: localhost`, `port: 5432`, `loadmethod: insert` (file loading can't reach a container). |
| Loader times out | `LoaderTimeoutSeconds` (default 600) in `run-sync.ps1`; big batches hang Tally: keep the loader's `batchsize` ≤ 10000. On timeout the whole process tree is killed (`taskkill /T /F`). |
| Tally slow for data-entry staff | Sync too frequent: raise `-IntervalSeconds`. |
| All runs marked FAILED on Windows | PowerShell 5.1 `ExitCode` quirk (`$null = $p.Handle` must follow `Start-Process`; already in the script). Run `.\sync\run-sync.ps1 -Once` and read the output. |
| Queries time out right after a big sync | Stale planner statistics. `fn_ingest` ends with `ANALYZE`; if you load data another way, run `ANALYZE` on the `tally.*` tables. |
| Reports empty but sync ok | Group hierarchy (`v_ledger_group.group_path`), see `tally-domain`. |
| `NOTICE: loader schema has no mst_opening_bill_allocation` | Optional table missing, so opening bill dues are not loaded. Check the loader's export config includes it. |

## Rebuilding

The mirror is disposable: Tally is the system of record. If it looks wrong, run a **full** sync (set `"sync": "full"` in the loader config once, run, switch back to `incremental`). The audit log is NOT rebuildable: never drop it. Back it up (`scripts/backup.ps1`).

## Verify a sync end to end

```sql
SELECT tally.fn_ingest(1, 'loader_c1', 'Company Name');   -- as tally_owner, returns rows copied
SELECT count(*) FROM tally.trn_voucher WHERE company_id = 1;
SELECT SUM(debit) - SUM(credit) FROM tally.fn_trial_balance(1, current_date);   -- should be 0.00
```

Then compare the trial balance totals and a few ledgers with Tally's own report. This is the real acceptance test (step E5 of `SETUP-WINDOWS.md`).
