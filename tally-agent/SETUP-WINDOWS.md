# Setting up Tally Agent on the Windows PC, step by step

This guide sets everything up on the PC that runs Tally, the one with the RTX 5060 Ti. It takes about 2–3 hours the first time, most of it downloads.

Run every command in **PowerShell**. Where a step says *(as Administrator)*, right-click PowerShell and choose **Run as administrator**.

| Part | What you do | Time |
|---|---|---|
| A | Put the code on GitHub | 10 min |
| B | Install the prerequisites | 45 min |
| C | Configure secrets and users | 15 min |
| D | Start it and try it with demo data | 30 min, mostly downloading the model |
| E | Connect your real Tally company | 30 min |
| F | Lock it down | 10 min |
| G | Backups | 10 min |
| H | Telegram bot (optional) | 20 min |

---

## Part A: Put the code on GitHub

Claude couldn't push because GitHub refused it access. You can push it yourself from your PC instead.

1. Install Git, *(as Administrator)*:
   ```powershell
   winget install --id Git.Git -e
   ```
   Close and reopen PowerShell afterwards.
2. On github.com, create a **new private repository** named `tally-agent` under `thenexgencfo`. Leave it empty: no README and no .gitignore.
3. Extract `tally-agent.zip` to `C:\`, so you end up with `C:\tally-agent\README.md`.
4. Push it:
   ```powershell
   cd C:\tally-agent
   git init
   git add .
   git commit -m "Initial import of Tally Agent"
   git branch -M main
   git remote add origin https://github.com/thenexgencfo/tally-agent.git
   git push -u origin main
   ```
   A browser window opens the first time so you can sign in to GitHub.

> To let Claude push in future sessions, connect GitHub at https://claude.ai/connect-github and install the Claude GitHub App on the repository.

Files that hold secrets (`deploy\.env`, `config\users.json`, `backups\`) are listed in `.gitignore`, so they are never pushed.

---

## Part B: Install the prerequisites

### B1. Tally: turn on the XML server
In TallyPrime: **F1 › Settings › Connectivity › Client/Server configuration**
- TallyPrime acts as: **Both**
- Port: **9000**

**Check:** with Tally open, browse to <http://localhost:9000>. It should say *TallyPrime Server is Running*.

### B2. NVIDIA driver
Install the latest **Game Ready or Studio driver** for the RTX 5060 Ti from nvidia.com. The RTX 50 series needs a recent driver.

**Check:**
```powershell
nvidia-smi
```
You should see the RTX 5060 Ti with 16 GB.

### B3. WSL2 and Docker Desktop
```powershell
# (as Administrator)
wsl --install
```
Restart the PC. Then install Docker Desktop:
```powershell
# (as Administrator)
winget install --id Docker.DockerDesktop -e
```
Open Docker Desktop, then go to **Settings**:
- **General:** tick *Use the WSL 2 based engine* and *Start Docker Desktop when you sign in*.
- **Resources › WSL integration:** turn it on.

**Check that Docker can see the GPU:**
```powershell
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
```
You should see the same GPU table as in B2. If you don't, stop here and fix the driver and WSL first.

### B4. Tailscale
```powershell
winget install --id Tailscale.Tailscale -e
```
Sign in to your firm's tailnet, then note this PC's Tailscale IP:
```powershell
tailscale ip -4
```
It looks like `100.x.y.z`. Write it down; this guide calls it **TS_IP**.

### B5. Node.js (needed by the Tally loader) and age (for encrypted backups)
```powershell
winget install --id OpenJS.NodeJS.LTS -e
winget install --id FiloSottile.age -e
```
Close and reopen PowerShell, then check:
```powershell
node --version
age --version
```

### B6. Let the project's scripts run
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
Get-ChildItem C:\tally-agent -Recurse | Unblock-File
```

### B7. Encrypt the disk (BitLocker)
The database files (a copy of your clients' books) and Tally's own data sit on this PC's disk. If the PC or its disk is stolen, encryption is what keeps them unreadable.
- **Settings › Privacy & security › Device encryption** → On. Or, on Windows Pro: **Control Panel › BitLocker Drive Encryption › Turn on BitLocker** for the C: drive (and D: if you keep backups there).
- Save the **recovery key** somewhere off this PC (print it, or keep it with the backup key from Part G).

### B8. Keep the PC awake
The PC must stay on and signed in, because Tally and Docker Desktop run in your session.
- **Settings › System › Power:** set *Sleep* to **Never** while plugged in.
- Lock the screen when you walk away (Win + L). Locking is fine; signing out or shutting down stops the service.

---

## Part C: Configure secrets and users

### C1. Create `deploy\.env`
```powershell
cd C:\tally-agent\deploy
Copy-Item .env.example .env
notepad .env
```
You need five secrets. Run this once per secret and paste each result into `.env`:
```powershell
$b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | % { $_.ToString('x2') })
```
Fill in:

| Line in `.env` | Value |
|---|---|
| `POSTGRES_SUPERUSER_PASSWORD=` | secret 1 |
| `RO_PASS=` | secret 2 |
| `OWNER_PASS=` | secret 3 |
| `REDIS_PASSWORD=` | secret 4 |
| `AUDIT_HASH_SALT=` | secret 5 |
| `CADDY_BIND_IP=` | your **TS_IP** |
| `AGENT_HOSTNAME=` | your **TS_IP** again |

For the **first trial run only**, also set:
```
SEED_DEMO_DATA=1
REFERENCE_DATE=2025-03-31
```
This loads a fake demo company so you can try the system before connecting Tally. You'll remove it in step D6.

### C2. Create the users file
```powershell
cd C:\tally-agent
Copy-Item config\users.example.json config\users.json
.\scripts\new-api-key.ps1
```
The script prints two lines:
- **API key:** give this to the person. It's shown only once, so send it securely, for example in person.
- **key_sha256:** paste this into that person's entry in `config\users.json`.

Run the script once per person, and edit `notepad config\users.json` to match your staff:

| Field | Meaning |
|---|---|
| `id` | A name for the audit log, such as `partner.sharma` |
| `role` | `partner`, `senior` or `junior`. Juniors can't see the party details report (GSTIN, PAN, contact) |
| `companies` | Which Tally companies they can see, such as `[1]` |
| `may_unmask` | `true` shows real party names; `false` shows "Party 1" |
| `telegram_id` | Only needed for the Telegram bot (Part H) |

Changes to this file take effect immediately, with no restart. To remove someone's access, delete their entry.

---

## Part D: Start it and try it

### D1. Download the AI model
This is about 9 GB and the only step that needs the internet. When running, the model has no internet access at all.
```powershell
cd C:\tally-agent\deploy
docker compose --env-file .env --profile setup run --rm ollama-pull
```

### D2. Start everything
```powershell
docker compose --env-file .env up -d --build
docker compose ps
```
Wait about a minute, then run `docker compose ps` again. You should see `postgres`, `redis`, `ollama`, `agent` and `caddy`, with `agent` showing **(healthy)**.

### D3. Open it in the browser
Browse to `https://TS_IP/`. The browser shows a certificate warning; that's expected for now, and step D5 removes it.

If you get **Forbidden**, which is likely on the first try, Docker is hiding visitors' real addresses. This is known and expected. Fix it:
```powershell
docker compose --env-file .env logs caddy | Select-String '"remote_ip":"[^"]*"' | Select-Object -Last 1
```
That shows something like `"remote_ip":"172.19.0.1"`. In `.env`, add the matching range to `ALLOWED_CIDRS`, for example:
```
ALLOWED_CIDRS=100.64.0.0/10 fd7a:115c:a1e0::/48 127.0.0.1/8 172.16.0.0/12
```
Then restart Caddy:
```powershell
docker compose --env-file .env up -d caddy
```
Part F's firewall rule is what actually keeps strangers out, so this is safe.

### D4. Sign in and ask
1. Paste your API key into the sign-in box.
2. The company shows as *White Design Consultants* (the demo data).
3. Try these questions:
   - `How much does Acme owe us?` → ₹1,50,000.00 Dr
   - `receivables ageing` → net due ₹4,63,000.00
   - `net profit for FY 2024-25` → ₹1,49,000.00
   - `cash and bank balance`
4. Try the **Reports (no AI)** tab too. It works even if the GPU is off.

The first AI question can take 20–40 seconds while the model loads onto the GPU. After that, expect a few seconds per question.

**Check the system's health at any time:** browse to `https://TS_IP/healthz`. `"status":"ok"` means everything is up, `"degraded"` means the AI model or sync needs attention, and `"down"` means the database or cache has stopped.

### D5. Remove the certificate warning on staff devices
Export Caddy's certificate:
```powershell
cd C:\tally-agent\deploy
docker compose --env-file .env cp caddy:/data/caddy/pki/authorities/local/root.crt ..\caddy-root.crt
```
Then on each staff device that will use the system:
- **Windows:** double-click `caddy-root.crt` › *Install Certificate* › *Current User* › *Place all certificates in* › **Trusted Root Certification Authorities**.
- **Android:** *Settings › Security › Encryption & credentials › Install a certificate › CA certificate*.
- **iPhone:** send the file to the phone and install the profile, then turn it on under *Settings › General › About › Certificate Trust Settings*.

Every staff device also needs **Tailscale installed and signed in** to the firm's tailnet.

### D6. Remove the demo data
In `.env`, set:
```
SEED_DEMO_DATA=0
REFERENCE_DATE=
```
Then wipe only the database (the 9 GB model stays):
```powershell
cd C:\tally-agent\deploy
docker compose --env-file .env down
docker volume rm tally-agent_pgdata
docker compose --env-file .env up -d
```

---

## Part E: Connect your real Tally company

### E1. Create a login for the company's sync
```powershell
cd C:\tally-agent
.\scripts\add-company.ps1 -CompanyId 1
```
Copy the **password** it prints.

### E2. Install the Tally loader
1. Download the latest release of **tally-database-loader** from <https://github.com/dhananjay1405/tally-database-loader/releases>.
2. Extract it to `C:\tally-agent\loader\c1\`, so that `run.bat` ends up in that folder.
3. Copy the example config into it:
   ```powershell
   Copy-Item C:\tally-agent\sync\loader-config.example.json C:\tally-agent\loader\c1\config.json
   notepad C:\tally-agent\loader\c1\config.json
   ```
4. In `config.json`, set:
   - `"password"`: the password from E1.
   - `"company"`: the company name **exactly as Tally shows it**, for example `White Design Consultants`.
   - Leave `"loadmethod": "insert"` as it is.

### E3. Run the loader once by hand
Keep Tally open with the company loaded, then:
```powershell
cd C:\tally-agent\loader\c1
.\run.bat
```
When it finishes, read `import-log.txt`. If `error-log.txt` has anything in it, fix that first. The loader's own README explains its error messages.

### E4. Copy the data into the agent
```powershell
cd C:\tally-agent
Copy-Item sync\companies.example.json sync\companies.json
notepad sync\companies.json
```
Set `name` to the same company name, and `fy_from` and `fy_to` to the financial year. Then:
```powershell
.\sync\run-sync.ps1 -Once
```
Expected output: `company 1: ok, <number> rows`.

### E5. Check the numbers against Tally
This is the most important check. In the web UI, ask `trial balance as on 31 March 2025` (use your own year end). Compare the **total debit, total credit and a few ledgers** with Tally's own trial balance. They must match exactly.

Then ask a few questions you already know the answers to: a party balance, the ageing report, net profit. If anything differs, note the question and the figures before relying on the system.

### E6. Keep syncing automatically
```powershell
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\tally-agent\sync\run-sync.ps1"'
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# No time limit (the default kills it after 3 days), restart if it ever stops, ignore battery.
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
       -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Tally Agent Sync" -Action $a -Trigger $t -Settings $s
Start-ScheduledTask -TaskName "Tally Agent Sync"
```
The agent now syncs about once a minute. The badge in the web UI shows how fresh the data is, and logs are in `C:\tally-agent\sync\logs\`.

**More companies:** repeat E1–E4 with `-CompanyId 2`, folder `loader\c2`, and a second entry in `companies.json`. Then add `2` to the `companies` list of each user who may see it.

---

## Part F: Lock it down

### F1. Windows Firewall *(as Administrator)*
```powershell
# Allow the web page only over Tailscale
New-NetFirewallRule -DisplayName "Tally Agent HTTPS (Tailscale only)" -Direction Inbound `
  -Protocol TCP -LocalPort 443 -InterfaceAlias "Tailscale" -Action Allow

# Tally's XML port has NO password. Anyone on your office network could read
# or change your books through it. Block it from the network; the loader uses
# localhost, which this rule does not affect.
New-NetFirewallRule -DisplayName "Block Tally XML port from network" -Direction Inbound `
  -Protocol TCP -LocalPort 9000 -Action Block
```

### F2. Tailscale access rules
In the Tailscale admin console, under **Access controls**, allow only staff devices (and the n8n server, if you use Part H) to reach this PC on port 443.

### F3. Check
- From a staff phone on mobile data with Tailscale on, `https://TS_IP/` should open.
- With Tailscale off, it shouldn't open.

---

## Part G: Backups

The Tally copy inside the agent can always be rebuilt from Tally. The **audit log can't**: it is the record of who looked at which client's data. These steps back it up, encrypted.

1. Create a backup key:
   ```powershell
   age-keygen -o C:\tally-agent-backup-key.txt
   ```
   It prints a **public key** starting with `age1...`. Copy it.
2. **Move `C:\tally-agent-backup-key.txt` off this PC:** to a USB drive kept in the office safe, or print it. Without it, backups can't be restored. If it stays on this PC, a thief gets the backups and the key together.
3. Save the public key and schedule nightly backups:
   ```powershell
   [Environment]::SetEnvironmentVariable("TALLY_BACKUP_AGE_RECIPIENT", "age1PASTE_YOURS", "User")

   $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\tally-agent\scripts\backup.ps1" -OutDir "D:\TallyAgentBackups"'
   $t = New-ScheduledTaskTrigger -Daily -At 2am
   Register-ScheduledTask -TaskName "Tally Agent Backup" -Action $a -Trigger $t
   ```
   Use a different disk for `-OutDir`, and ideally copy the backups somewhere off-site as well.
4. **Every 3 months, test a restore (a "drill"):**
   ```powershell
   .\scripts\restore.ps1 -Backup D:\TallyAgentBackups\tally-YYYYMMDD-HHMM.dump.age -AgeIdentity E:\tally-agent-backup-key.txt
   ```
   This restores into a temporary, separate database, checks it, and deletes it. **Your live system is not touched.** It should print `DRILL PASSED` with the number of audit rows. A backup that has never been restored is not proven to work.

   Only in a real disaster (the database is lost) add `-ReallyReplaceProduction`. That replaces the live database, and first takes a fresh backup of whatever is there now, so recent audit records are not lost.

---

## Part H: Telegram bot (optional, uses your existing n8n)

Telegram is a third-party service, so the bridge is set up to **always show masked names ("Party 1")**, whatever the person's role.

1. In Telegram, message **@BotFather** with `/newbot` and copy the bot token.
2. Run `.\scripts\new-api-key.ps1` for the bridge. Put the hash in the `svc.n8n-telegram` entry of `users.json`, and keep `"force_masked": true`.
3. For each staff member who will use the bot: they message **@userinfobot** to get their numeric Telegram ID, and you put it in their `telegram_id` in `users.json`.
4. In n8n, import `n8n\tally-agent-telegram-bot.json` and `n8n\tally-agent-health-alert.json`. Then:
   - Create a **Telegram** credential with the bot token.
   - Create a **Header Auth** credential with name `Authorization` and value `Bearer <bridge API key>`.
   - In both workflows, change `https://tally-agent.internal` to `https://TS_IP`.
   - In the alert workflow, set your own chat ID.
   - So n8n trusts the certificate, copy `caddy-root.crt` to the n8n server and set `NODE_EXTRA_CA_CERTS=/path/to/caddy-root.crt` in its environment.
5. Activate both workflows and message the bot: `receivables ageing`.

The bot answers only in private chats, never in groups. It uses the person's first company; to ask about another one, start the message with `/c 2` (company 2), e.g. `/c 2 receivables ageing`.

---

## Everyday tasks

| Task | How |
|---|---|
| Add a person | `.\scripts\new-api-key.ps1`, then add an entry to `config\users.json`. Takes effect immediately |
| Remove a person | Delete their entry in `config\users.json`. Immediate |
| Check health | `https://TS_IP/healthz` |
| See who asked what | `docker exec tally-agent-postgres-1 psql -U postgres -d tally -c "SELECT ts, user_id, report_id, outcome FROM audit.query_log ORDER BY ts DESC LIMIT 20"` |
| Restart everything | `cd C:\tally-agent\deploy; docker compose --env-file .env restart` |
| Update the code | `cd C:\tally-agent; git pull; cd deploy; docker compose --env-file .env up -d --build` |

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Page shows **Forbidden** | Step D3: add the `remote_ip` range to `ALLOWED_CIDRS` |
| Page doesn't load at all | Tailscale is off on your device, `AGENT_HOSTNAME` doesn't match the address you typed, or Docker Desktop isn't running |
| Certificate error after D5 | You installed the certificate under *Personal* instead of *Trusted Root Certification Authorities* |
| "Synced N minutes ago" warning | Tally is closed, or the sync task stopped. Check `sync\logs\` and run `.\sync\run-sync.ps1 -Once` |
| "Last Tally sync FAILED" | Read `loader\c1\error-log.txt`. The company name in `config.json` must match Tally exactly |
| "access list is being updated or is invalid" for everyone | `config\users.json` has a mistake (a missing comma, a misspelt field). Fix it; access returns immediately. `https://TS_IP/healthz` shows `"users_file":"invalid"` until then |
| Answers come from "keyword routing" | The AI model is down. Run `docker compose --env-file .env logs ollama` and check `nvidia-smi` |
| First question is slow | Normal: the model is loading onto the GPU. It stays loaded for 30 minutes |
| `docker compose` complains about a missing value | A line in `.env` is empty. Fill in every line from C1 |
| Scripts won't run ("not digitally signed") | Run step B6 again |
