<#
.SYNOPSIS
  Test (default) or perform a restore of an encrypted Tally Agent backup.

.DESCRIPTION
  DEFAULT = DRILL. The backup is restored into a throwaway PostgreSQL container
  with no network, checked, and the container is deleted. Production is not
  touched. Do this every quarter.

  -ReallyReplaceProduction replaces the LIVE database. Restoring an older backup
  would delete every audit-log row written since it was taken, so this first
  takes a fresh encrypted backup of the current database (via backup.ps1) and
  aborts if that fails.

.EXAMPLE
  # Quarterly drill (safe):
  .\scripts\restore.ps1 -Backup D:\TallyAgentBackups\tally-20261104-0200.dump.age -AgeIdentity E:\tally-agent-backup-key.txt

.EXAMPLE
  # Real disaster recovery:
  .\scripts\restore.ps1 -Backup ... -AgeIdentity ... -ReallyReplaceProduction
#>
param(
  [Parameter(Mandatory)][string]$Backup,
  [Parameter(Mandatory)][string]$AgeIdentity,
  [switch]$ReallyReplaceProduction,
  [string]$PostgresContainer = "tally-agent-postgres-1",
  [string]$PostgresImage = "postgres:16.6-bookworm"
)
$ErrorActionPreference = "Stop"
if (-not (Get-Command age -ErrorAction SilentlyContinue)) { throw "age.exe not found on PATH." }
$plain = Join-Path $env:TEMP ("tally-restore-{0}.dump" -f ([guid]::NewGuid().ToString("N")))
try {
  & age -d -i $AgeIdentity -o $plain $Backup
  if ($LASTEXITCODE -ne 0) { throw "decryption failed" }

  if (-not $ReallyReplaceProduction) {
    # ---------------------------------------------------------------- DRILL
    $drill = "tally-restore-drill"
    docker rm -f $drill 2>$null | Out-Null
    $b = New-Object byte[] 16; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    $pw = -join ($b | ForEach-Object { $_.ToString("x2") })
    docker run -d --name $drill --network none -e "POSTGRES_PASSWORD=$pw" $PostgresImage | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not start drill container" }
    try {
      $ready = $false
      for ($i = 0; $i -lt 60; $i++) {
        docker exec $drill pg_isready -U postgres -q 2>$null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
      }
      if (-not $ready) { throw "drill database did not start" }
      Start-Sleep -Seconds 2   # the image restarts once after first init
      docker exec $drill createdb -U postgres tally
      docker cp $plain "${drill}:/tmp/r.dump"
      docker exec $drill pg_restore -U postgres -d tally --no-owner --no-privileges --exit-on-error /tmp/r.dump
      if ($LASTEXITCODE -ne 0) { throw "DRILL FAILED: the backup did not restore cleanly" }
      $check = docker exec $drill psql -U postgres -d tally -qtA -c `
        "SELECT 'audit rows: ' || count(*) || ', newest: ' || coalesce(max(ts)::text,'none') FROM audit.query_log; SELECT 'companies: ' || count(*) FROM tally.mst_company;"
      Write-Host "DRILL PASSED. Restored copy contains:"
      $check | ForEach-Object { Write-Host "  $_" }
      Write-Host "Production was not touched."
    } finally {
      docker rm -f $drill 2>$null | Out-Null
    }
    return
  }

  # ------------------------------------------------------------ PRODUCTION
  Write-Host "Taking a fresh backup of the CURRENT database first..."
  & (Join-Path $PSScriptRoot "backup.ps1") -PostgresContainer $PostgresContainer
  if (-not $?) { throw "pre-restore backup failed; production NOT replaced" }
  docker cp $plain "${PostgresContainer}:/tmp/tally-restore.dump"
  # --single-transaction: either the whole restore lands, or nothing changes.
  docker exec $PostgresContainer sh -c "pg_restore -U postgres -d tally --clean --if-exists --single-transaction --exit-on-error /tmp/tally-restore.dump"
  if ($LASTEXITCODE -ne 0) { throw "pg_restore failed - database unchanged" }
  Write-Host "Restored. The pre-restore backup is in the backups folder if you need the newer audit rows."
} finally {
  Remove-Item $plain -ErrorAction SilentlyContinue
  if ($ReallyReplaceProduction) { docker exec $PostgresContainer rm -f /tmp/tally-restore.dump 2>$null }
}
