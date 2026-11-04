<#
.SYNOPSIS  Restore an encrypted backup. Replaces the current database contents.
.EXAMPLE   .\scripts\restore.ps1 -Backup D:\backups\tally-20261104-0200.dump.age -AgeIdentity E:\age-key.txt -Confirm
#>
param(
  [Parameter(Mandatory)][string]$Backup,
  [Parameter(Mandatory)][string]$AgeIdentity,
  [switch]$Confirm,
  [string]$PostgresContainer = "tally-agent-postgres-1"
)
$ErrorActionPreference = "Stop"
if (-not $Confirm) { throw "This REPLACES the database. Re-run with -Confirm once you are sure." }
$plain = Join-Path $env:TEMP "tally-restore.dump"
try {
  & age -d -i $AgeIdentity -o $plain $Backup
  if ($LASTEXITCODE -ne 0) { throw "decryption failed" }
  docker cp $plain "${PostgresContainer}:/tmp/tally-restore.dump"
  # --single-transaction: either the whole restore lands, or nothing changes.
  docker exec $PostgresContainer sh -c "pg_restore -U postgres -d tally --clean --if-exists --single-transaction /tmp/tally-restore.dump"
  if ($LASTEXITCODE -ne 0) { throw "pg_restore failed - database unchanged" }
  Write-Host "Restored. Check: docker exec $PostgresContainer psql -U postgres -d tally -c 'SELECT count(*) FROM audit.query_log'"
} finally {
  Remove-Item $plain -ErrorAction SilentlyContinue
  docker exec $PostgresContainer rm -f /tmp/tally-restore.dump 2>$null
}
