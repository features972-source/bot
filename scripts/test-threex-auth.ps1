# Test 3CX API login. Usage:
#   powershell -File scripts\test-threex-auth.ps1          # bot 1 (.env)
#   powershell -File scripts\test-threex-auth.ps1 -EnvFile .env.bot2
param(
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path (Join-Path $Root $EnvFile))) {
    Write-Host "Missing $EnvFile"
    exit 1
}

Set-Location $Root
& $Python (Join-Path $Root "scripts\test_threex_auth.py") $EnvFile
