<#
.SYNOPSIS
  Tally -> PostgreSQL sync loop for the Tally Agent. Runs on the Windows PC with Tally.

.DESCRIPTION
  For each company in companies.json:
    1. run tally-database-loader once (its config has frequency 0, sync incremental)
    2. copy its output into the RLS-protected mirror with tally.fn_ingest()
  A failed run is recorded with tally.fn_mark_sync_failed(); the previous good
  snapshot stays in place and every answer shows the staleness warning.

  Register as a Scheduled Task, "At log on" of the account that runs Docker Desktop:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\tally-agent\sync\run-sync.ps1

.NOTES
  Reads Tally only through its XML port (via the loader). Never touches Tally's data folder.
#>
param(
  [string]$Config = (Join-Path $PSScriptRoot "companies.json"),
  [int]$IntervalSeconds = 60,
  [int]$LoaderTimeoutSeconds = 600,
  [string]$PostgresContainer = "tally-agent-postgres-1",
  [switch]$Once
)
$ErrorActionPreference = "Stop"
$LogDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Single instance: two overlapping runs would fight over the same loader schema.
$mutex = New-Object System.Threading.Mutex($false, "Global\TallyAgentSync")
if (-not $mutex.WaitOne(0)) { Write-Host "Another sync is already running."; exit 0 }

function Write-Log([string]$Message) {
  $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message
  Add-Content -Path (Join-Path $LogDir ("sync-{0:yyyyMMdd}.log" -f (Get-Date))) -Value $line
  Write-Host $line
}

function Invoke-Psql([string]$Sql, [hashtable]$Vars) {
  # Values travel as psql variables and are quoted by psql (:'name'), never
  # spliced into SQL text.
  $argv = @("exec", "-i", $PostgresContainer, "psql", "-U", "tally_owner", "-d", "tally",
            "-v", "ON_ERROR_STOP=1", "-qtA")
  foreach ($k in $Vars.Keys) { $argv += @("-v", "$k=$($Vars[$k])") }
  $out = $Sql | & docker @argv 2>&1
  if ($LASTEXITCODE -ne 0) { throw "psql failed: $out" }
  return ($out | Out-String).Trim()
}

function Invoke-Loader($Company) {
  $dir = $Company.loader_dir
  if (-not (Test-Path (Join-Path $dir "run.bat"))) { throw "run.bat not found in $dir" }
  $errLog = Join-Path $dir "error-log.txt"
  Remove-Item $errLog -ErrorAction SilentlyContinue
  $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "run.bat" -WorkingDirectory $dir `
         -NoNewWindow -PassThru
  if (-not $p.WaitForExit($LoaderTimeoutSeconds * 1000)) {
    $p.Kill()
    throw "loader timed out after $LoaderTimeoutSeconds s (large batch? see loader batchsize)"
  }
  $err = if (Test-Path $errLog) { (Get-Content $errLog -Raw) } else { "" }
  if ($p.ExitCode -ne 0 -or ($err -and $err.Trim())) {
    $snippet = if ($err) { $err.Substring(0, [Math]::Min(400, $err.Length)) } else { "" }
    throw "loader exit $($p.ExitCode): $snippet"
  }
}

$companies = Get-Content $Config -Raw | ConvertFrom-Json
foreach ($c in $companies) {
  if ($c.name -match '["\r\n]') { throw "company name for id $($c.company_id) contains a quote or newline" }
}

try {
  do {
    foreach ($c in $companies) {
      $started = Get-Date
      try {
        Invoke-Loader $c
        $rows = Invoke-Psql "SELECT tally.fn_ingest(:cid, :'sch', :'cname', NULLIF(:'fyf','')::date, NULLIF(:'fyt','')::date);" @{
          cid = [int]$c.company_id; sch = "loader_c$([int]$c.company_id)"; cname = $c.name
          fyf = "$($c.fy_from)"; fyt = "$($c.fy_to)" }
        Write-Log ("company {0}: ok, {1} rows, {2:n1}s" -f $c.company_id, $rows, ((Get-Date) - $started).TotalSeconds)
      } catch {
        $msg = $_.Exception.Message -replace '[\r\n]+', ' '
        Write-Log "company $($c.company_id): FAILED - $msg"
        try { Invoke-Psql "SELECT tally.fn_mark_sync_failed(:cid, :'err');" @{ cid = [int]$c.company_id; err = $msg } | Out-Null }
        catch { Write-Log "  could not record failure: $($_.Exception.Message)" }
      }
    }
    if (-not $Once) { Start-Sleep -Seconds $IntervalSeconds }
  } while (-not $Once)
} finally {
  $mutex.ReleaseMutex()
}
