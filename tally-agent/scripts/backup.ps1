<#
.SYNOPSIS
  Encrypted backup of the Tally Agent database and user list.

.DESCRIPTION
  The Tally mirror can always be rebuilt from Tally by a full re-sync. The AUDIT
  LOG cannot: it is the record of who saw which client's data, and losing it is
  a DPDP problem. This backs up the whole database (audit included) plus
  config\users.json, encrypted with age (https://age-encryption.org).

  Refuses to write an unencrypted backup. Keep the age PRIVATE key off this
  machine (e.g. printed and in the partner's safe, or on a separate device).

  Schedule nightly. Test a restore every quarter with restore.ps1.

.EXAMPLE
  $env:TALLY_BACKUP_AGE_RECIPIENT = "age1..."; .\scripts\backup.ps1 -OutDir D:\backups
#>
param(
  [string]$OutDir = (Join-Path $PSScriptRoot "..\backups"),
  [string]$AgeRecipient = $env:TALLY_BACKUP_AGE_RECIPIENT,
  [int]$KeepDays = 35,
  [string]$PostgresContainer = "tally-agent-postgres-1"
)
$ErrorActionPreference = "Stop"
if (-not $AgeRecipient) { throw "Set TALLY_BACKUP_AGE_RECIPIENT (age public key). Unencrypted backups are not allowed." }
if (-not (Get-Command age -ErrorAction SilentlyContinue)) { throw "age.exe not found on PATH." }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$plain = Join-Path $env:TEMP "tally-$stamp.dump"
try {
  # Dump inside the container and copy the file out: piping binary through
  # Windows PowerShell corrupts it.
  docker exec $PostgresContainer sh -c "pg_dump -U postgres -d tally -Fc -f /tmp/tally-backup.dump"
  if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
  docker exec $PostgresContainer sh -c "pg_restore -l /tmp/tally-backup.dump > /dev/null"
  if ($LASTEXITCODE -ne 0) { throw "backup failed verification (pg_restore -l)" }
  docker cp "${PostgresContainer}:/tmp/tally-backup.dump" $plain
  docker exec $PostgresContainer rm -f /tmp/tally-backup.dump

  & age -r $AgeRecipient -o (Join-Path $OutDir "tally-$stamp.dump.age") $plain
  if ($LASTEXITCODE -ne 0) { throw "encryption failed" }
  $users = Join-Path $PSScriptRoot "..\config\users.json"
  if (Test-Path $users) {
    & age -r $AgeRecipient -o (Join-Path $OutDir "users-$stamp.json.age") $users
  }
} finally {
  Remove-Item $plain -ErrorAction SilentlyContinue
}

Get-ChildItem $OutDir -Filter "*.age" |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
  Remove-Item
Write-Host "Backup written: tally-$stamp.dump.age"
