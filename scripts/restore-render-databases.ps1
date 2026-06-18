"""Restore Q1 and Q2 SQLite databases to separate Render Web Services."""

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Read-EnvValue($path, $key) {
    $line = Select-String -Path $path -Pattern "^$key=(.+)$" | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Matches.Groups[1].Value.Trim()
}

function Resolve-RenderBaseUrl($envPath, $fallback) {
    $listen = Read-EnvValue $envPath "LISTEN_PUBLIC_URL"
    if ($listen -and $listen -match "onrender\.com") {
        return $listen.TrimEnd("/")
    }
    return $fallback
}

$q1Url = Resolve-RenderBaseUrl (Join-Path $Root ".env") "https://bot-josl.onrender.com"
$q2Url = Resolve-RenderBaseUrl (Join-Path $Root ".env.bot2") ""

$q1Secret = Read-EnvValue (Join-Path $Root ".env") "WEBHOOK_SECRET"
$q2Secret = Read-EnvValue (Join-Path $Root ".env.bot2") "WEBHOOK_SECRET"

foreach ($pair in @(
        @{ Name = "Q1"; File = "links.db"; Secret = $q1Secret; BaseUrl = $q1Url },
        @{ Name = "Q2"; File = "links-bot2.db"; Secret = $q2Secret; BaseUrl = $q2Url }
    )) {
    $dbPath = Join-Path $Root $pair.File
    if (-not (Test-Path $dbPath)) {
        Write-Host "Skip $($pair.Name): missing $dbPath" -ForegroundColor Yellow
        continue
    }
    if (-not $pair.Secret) {
        Write-Host "Skip $($pair.Name): WEBHOOK_SECRET not found" -ForegroundColor Yellow
        continue
    }
    if (-not $pair.BaseUrl) {
        Write-Host "Skip $($pair.Name): set LISTEN_PUBLIC_URL in .env.bot2 to your Q2 Render URL" -ForegroundColor Yellow
        continue
    }
    $url = "$($pair.BaseUrl)/admin/restore-db?secret=$($pair.Secret)"
    Write-Host "Restoring $($pair.Name) to $($pair.BaseUrl) from $($pair.File) ..."
    $response = curl.exe -s -X POST $url -F "file=@$dbPath"
    Write-Host $response
}

Write-Host ""
Write-Host "Done. Check Q1: $q1Url/health" -ForegroundColor Green
if ($q2Url) {
    Write-Host "       Q2: $q2Url/health" -ForegroundColor Green
}
