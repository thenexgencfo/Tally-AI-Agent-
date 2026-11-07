---
name: windows-ops
description: Install, update, run, back up and restore the Tally AI Agent on the firm's Windows PC (Docker Desktop, Tailscale, Caddy, scheduled tasks, PowerShell scripts, age-encrypted backups). Use for any deployment or operations question, or when editing deploy/, sync/ or scripts/*.ps1.
---

# Windows operations

Authoritative step-by-step guide: `tally-agent/SETUP-WINDOWS.md` (Parts A to H). Don't duplicate it; keep it correct. Project folder on the PC: `C:\TallyAI\tally-agent`. Repo: `https://github.com/thenexgencfo/Tally-AI-Agent-`.

## Facts

- The PC runs TallyPrime Gold (XML server on 9000), Docker Desktop (WSL2, NVIDIA GPU for Ollama), Tailscale, Node.js (loader) and `age` (backups). It must stay on and signed in; the sync task starts at logon.
- Stack: Compose project `tally-agent`: `postgres`, `redis`, `ollama`, `agent`, `caddy`. Caddy binds to the Tailscale IP only (`CADDY_BIND_IP`) and `AGENT_HOSTNAME` is that same IP (a `*.ts.net` name makes Caddy try to fetch a certificate from Tailscale, unreachable in Docker). Staff devices must trust Caddy's internal CA and be on the tailnet.
- Networks: `backend` and `frontend` are internal (no internet). `edge` carries Caddy's port. `dbhost` carries Postgres's loopback port for the loader. `egress` exists only for the one-off model download (`--profile setup`).
- Secrets live in `deploy\.env` (never committed). Users in `config\users.json` (SHA-256 key hashes only; hot-reloaded; an invalid file refuses everyone).
- Windows Firewall must allow 443 on the Tailscale interface only, and **block 9000** from the LAN (Tally's XML port has no authentication).

## Routine commands (PowerShell, from `C:\TallyAI\tally-agent\deploy`)

```powershell
docker compose --env-file .env ps
docker compose --env-file .env logs --tail 100 agent
docker compose --env-file .env restart
cd C:\TallyAI; git pull; cd tally-agent\deploy; docker compose --env-file .env up -d --build    # update
```

Health: `https://<TS_IP>/healthz` → `status` ok / degraded (model or sync) / down (DB or Redis), `users_file`, `sync`.

## Scheduled tasks

- **Sync** ("Tally Agent Sync"): created with `New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)`. Without these the default kills it after 3 days.
- **Backup** ("Tally Agent Backup", nightly): `scripts\backup.ps1 -OutDir D:\TallyAgentBackups` with `TALLY_BACKUP_AGE_RECIPIENT` set. It refuses to write unencrypted backups. The age **private** key must live off the PC.

## Backups and restore

- The mirror can be rebuilt from Tally. The **audit log cannot**, so back up the whole DB (`pg_dump -Fc`), encrypted with `age`.
- `scripts\restore.ps1 -Backup ... -AgeIdentity ...` is a **drill by default**: it restores into a throwaway container with no network, checks it, and deletes it. `-ReallyReplaceProduction` first takes a fresh backup of the live DB, then replaces it. Never test restores against production.

## Editing the PowerShell (target: Windows PowerShell 5.1)

- No `RandomNumberGenerator.Fill` (use `Create().GetBytes`). Quote native-command arguments carefully: PS 5.1 doesn't escape embedded quotes, so pass any free text as **base64** and decode in SQL (`convert_from(decode(:'x','base64'),'UTF8')`) as `run-sync.ps1` does. After `Start-Process -PassThru`, do `$null = $p.Handle` before reading `ExitCode`. Kill process trees with `taskkill /T /F`.
- `.ps1` files are CRLF (`.gitattributes`); `.sh` and `.sql` must stay LF or the Postgres init script fails.
- Use `$ErrorActionPreference = "Stop"` and clean up temp files in `finally`.
- **These scripts have never been run on Windows from this repository.** When you change one, say so, and ask the user to run it once by hand and report the output.

## Troubleshooting quick table

| Symptom | Check |
|---|---|
| Page says Forbidden | Caddy sees Docker's bridge address: add its `remote_ip` range to `ALLOWED_CIDRS` (SETUP-WINDOWS D3) |
| Page doesn't load | Tailscale off, `AGENT_HOSTNAME` ≠ address typed, Docker Desktop not running |
| Answers via "keyword routing" | Model down: `docker compose logs ollama`, `nvidia-smi`, was `qwen3:14b` pulled |
| 503 for everyone, "access list invalid" | typo in `config\users.json` (`/healthz` shows `users_file: invalid`) |
| "Synced N minutes ago" | sync task stopped or Tally closed: see `sync-troubleshooting` |
