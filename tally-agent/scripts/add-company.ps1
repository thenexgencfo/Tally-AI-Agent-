<#
.SYNOPSIS  Provision a Tally company for syncing: its own loader login + schema.
.EXAMPLE   .\scripts\add-company.ps1 -CompanyId 1
#>
param(
  [Parameter(Mandatory)][int]$CompanyId,
  [string]$PostgresContainer = "tally-agent-postgres-1"
)
$ErrorActionPreference = "Stop"
$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$pass = -join ($bytes | ForEach-Object { $_.ToString("x2") })
$role = "loader_c$CompanyId"
docker exec $PostgresContainer psql -U postgres -d tally -q -v "role=$role" -v "pass=$pass" `
  -f /opt/tally-agent/sql/admin/add_loader_company.sql
if ($LASTEXITCODE -ne 0) { throw "provisioning failed" }
Write-Host ""
Write-Host "Loader login for company $CompanyId (shown once - put it in that loader's config.json):"
Write-Host "  username: $role"
Write-Host "  password: $pass"
Write-Host ""
Write-Host "Then add the company to sync\companies.json and to the users who may see it in config\users.json."
