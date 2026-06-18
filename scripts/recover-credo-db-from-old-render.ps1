# Recover links.db from suspended bot-josl and restore to q1-call-manager-eu.
param(
    [string]$OldUrl = "https://bot-josl.onrender.com",
    [string]$NewUrl = "https://q1-call-manager-eu.onrender.com",
    [string]$OldServiceId = "srv-d8pmli4vikkc739pkuj0",
    [string]$NewServiceId = "srv-d8pvmemrnols73d31gpg",
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Resolve-Path (Join-Path $PSScriptRoot "..") }
if (-not $env:RENDER_API_KEY) { throw "Set RENDER_API_KEY" }

$Headers = @{
    Authorization  = "Bearer $env:RENDER_API_KEY"
    Accept         = "application/json"
    "Content-Type" = "application/json"
}

function Wait-Health([string]$BaseUrl, [int]$Minutes = 15) {
    $url = "$($BaseUrl.TrimEnd('/'))/health"
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ((Get-Date) -lt $deadline) {
        Write-Host "Waiting for $url ..."
        try {
            $h = Invoke-RestMethod -Uri $url -TimeoutSec 45
            if ($h.ok) { return $h }
        } catch { Write-Host "  $($_.Exception.Message)" }
        Start-Sleep -Seconds 20
    }
    throw "Timed out: $url"
}

$secret = (Select-String -Path (Join-Path $Root ".env") -Pattern '^WEBHOOK_SECRET=(.+)$').Matches.Groups[1].Value.Trim()
$outPath = Join-Path $Root "links-recovered.db"

Write-Host "Suspending new service ..."
Invoke-RestMethod -Method POST -Uri "https://api.render.com/v1/services/$NewServiceId/suspend" -Headers $Headers | Out-Null

Write-Host "Resuming old service ..."
Invoke-RestMethod -Method POST -Uri "https://api.render.com/v1/services/$OldServiceId/resume" -Headers $Headers | Out-Null

Write-Host "Deploying latest code to old service (needs /admin/export-db) ..."
Invoke-RestMethod -Method POST -Uri "https://api.render.com/v1/services/$OldServiceId/deploys" -Headers $Headers -Body '{"clearCache":"do_not_clear"}' | Out-Null
Wait-Health $OldUrl | Out-Null

$exportUrl = "$($OldUrl.TrimEnd('/'))/admin/export-db?secret=$secret"
Write-Host "Downloading database from old service ..."
curl.exe -s -f -o $outPath $exportUrl
if (-not (Test-Path $outPath) -or (Get-Item $outPath).Length -lt 1000) {
    throw "Export failed or file too small: $outPath"
}
Write-Host "Downloaded $((Get-Item $outPath).Length) bytes -> $outPath"

Write-Host "Suspending old service ..."
Invoke-RestMethod -Method POST -Uri "https://api.render.com/v1/services/$OldServiceId/suspend" -Headers $Headers | Out-Null

Write-Host "Resuming new service ..."
Invoke-RestMethod -Method POST -Uri "https://api.render.com/v1/services/$NewServiceId/resume" -Headers $Headers | Out-Null
Invoke-RestMethod -Method POST -Uri "https://api.render.com/v1/services/$NewServiceId/deploys" -Headers $Headers -Body '{"clearCache":"do_not_clear"}' | Out-Null
Wait-Health $NewUrl | Out-Null

Write-Host "Restoring recovered database to new service ..."
$restore = curl.exe -s -X POST "$($NewUrl.TrimEnd('/'))/admin/restore-db?secret=$secret" -F "file=@$outPath"
Write-Host $restore

$health = Invoke-RestMethod -Uri "$($NewUrl.TrimEnd('/'))/health"
Write-Host "payments_logged=$($health.payments_logged)"
Copy-Item -Force $outPath (Join-Path $Root "links.db")
Write-Host "Done. Copied recovered DB to links.db"
