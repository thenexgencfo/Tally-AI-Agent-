---
name: test-and-verify
description: Run, write and interpret tests for the Tally AI Agent: the pytest suite, messy-data and outage tests, the container smoke test, and how to prove a change works. Use before claiming anything is fixed or done.
---

# Test and verify

Work from `tally-agent/`. Never say "fixed" or "passing" without showing the run.

## The suite

```bash
./scripts/test.sh -q -p no:warnings     # rebuild the demo DB + run everything (about 15 s, about 167 tests)
```

- Needs local PostgreSQL 16 + Redis. If Postgres is down the DB tests **skip silently**. Always read the pass count, and start the services first: `sudo service postgresql start`, `redis-server --daemonize yes --port 6379 --save '' --appendonly no`.
- `tests/conftest.py` sets env (`PG_DSN`, Redis DB 15, a temp `users.json`, `REFERENCE_DATE=2025-03-31`, `LLM_ENABLED=false`) and provides: `KEYS` / `user("partner"|"senior"|"junior"|"other")`, `FakeLLM(plan=..., narrative=..., fail=...)` which **records every prompt**, `rules_router`, `redis_client`, `dbready`, `owner_con` (a `tally_owner` connection for setup and audit assertions).
- Demo company 1 ("White Design Consultants") is hand-checked: trial balance ₹10,51,000 each side, net profit ₹1,49,000, receivables net ₹4,63,000. Company 2 exists to prove isolation. `sql/99_seed_demo.sql` is the source.

| File | Covers |
|---|---|
| `test_recognizers.py`, `test_masking.py` | patterns, name index, vault, role display |
| `test_reports_db.py` | every report's figures, RLS, privileges, audit append-only, ingest attacks, accounting edge cases (`co600` fixture) |
| `test_planner.py` | date resolution, routing, model-output validation |
| `test_security_e2e.py` | the whole pipeline with a recording model: nothing reaches the model, injection, audit, isolation |
| `test_http.py` | auth, delegation, rate limit, headers, 503s, users-file reload |
| `test_robustness.py` + `messy_data.py` | production-like messy company, hostile input, concurrency, hung model |
| `test_answer_and_live.py` | summaries, grounding, Tally XML client |

## Writing tests

- **Bugs:** write the failing test first, then fix.
- **Security attacks:** write the attack as a test (planted view in a loader schema, cross-company request, prompt injection in a narration). It must fail loudly if the control is removed.
- **Real-world data:** extend `tests/messy_data.py` (Unicode names, cyclic groups, NULLs, deleted ledgers, leap day, several financial years) rather than only the tidy seed.
- Clean up what you create: company ids >= 500 are for tests; delete them in a `finally`/fixture teardown.
- Don't use `rules_router` for tests with dates other than 2025-03-31 (hard-coded).

## Beyond pytest

- **Run the app and ask it things** (see the commands in `CLAUDE.md`); `curl -H "Authorization: Bearer <key>" -d '{"question":"receivables ageing"}' localhost:8080/v1/ask`.
- **Container smoke test:** `cd deploy`, make a `.env` (see `.env.example`; `SEED_DEMO_DATA=1`, `REFERENCE_DATE=2025-03-31`, `LLM_ENABLED=false`, `CADDY_BIND_IP=127.0.0.1`, `AGENT_HOSTNAME=127.0.0.1`), `docker compose --env-file .env up -d --build`, then check `/healthz`, an answer over HTTPS (trust Caddy's root: `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .`), and that the agent has no default route (`docker compose exec agent cat /proc/net/route`). Tear down with `down -v`.
- **Outages:** stop Redis, then Postgres; every request must return a clear JSON 503 and recover by itself.

## Reporting honestly

State what ran, what passed (with the count), and what you could NOT run (real model, real Tally data, Windows PowerShell, n8n). Don't describe untested things as working.
