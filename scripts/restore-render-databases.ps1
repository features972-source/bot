"""Restore Q1 and Q2 SQLite databases to Render (same service, separate files on /data)."""

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Read-EnvValue($path, $key) {
    $line = Select-String -Path $path -Pattern "^$key=(.+)$" | Select-Object -First 1
    if (-not $line) { return $null }
    return $line.Matches.Groups[1].Value.Trim()
}

$baseUrl = "https://bot-josl.onrender.com"
$listen = Read-EnvValue (Join-Path $Root ".env") "LISTEN_PUBLIC_URL"
if ($listen -and $listen -match "onrender\.com") {
    $baseUrl = $listen.TrimEnd("/")
}

$q1Secret = Read-EnvValue (Join-Path $Root ".env") "WEBHOOK_SECRET"
$q2Secret = Read-EnvValue (Join-Path $Root ".env.bot2") "WEBHOOK_SECRET"

foreach ($pair in @(
        @{ Name = "Q1"; File = "links.db"; Secret = $q1Secret; Instance = "q1" },
        @{ Name = "Q2"; File = "links-bot2.db"; Secret = $q2Secret; Instance = "q2" }
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
    $url = "$baseUrl/admin/restore-db?secret=$($pair.Secret)&instance=$($pair.Instance)"
    Write-Host "Restoring $($pair.Name) from $($pair.File) ..."
    $response = curl.exe -s -X POST $url -F "file=@$dbPath"
    Write-Host $response
}

Write-Host ""
Write-Host "Done. Redeploy or wait ~30s, then check $baseUrl/health" -ForegroundColor Green
