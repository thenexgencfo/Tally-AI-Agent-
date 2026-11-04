<#
.SYNOPSIS  Generate a staff API key. Give the key to the person; store only the hash.
#>
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$key = "tak_" + ([Convert]::ToBase64String($bytes) -replace '\+', '-' -replace '/', '_' -replace '=', '')
$sha = [Security.Cryptography.SHA256]::Create()
$hash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key)) | ForEach-Object { $_.ToString("x2") })
Write-Host "API key (give to the user, shown once): $key"
Write-Host "key_sha256 (put in config\users.json):  $hash"
