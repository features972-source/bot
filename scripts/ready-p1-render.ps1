# Fix P1 on Render: sync secrets from .env.press1 and/or suspend duplicate broken services.
#
# Usage:
#   $env:RENDER_API_KEY = "rnd_..."   # Render -> Account Settings -> API Keys
#   powershell -File scripts/ready-p1-render.ps1
#
# Keeps the healthy P1 service (p1-bot-m9an) and suspends extra p1-* services missing BOT_TOKEN.

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvFile = Join-Path $Root ".env.press1"
$SshKeyFile = Join-Path $Root "p1-telegram-bot\RENDER_SSH_KEY_ONE_LINE.txt"

if (-not $env:RENDER_API_KEY) {
    Write-Host "Set RENDER_API_KEY first (Render dashboard -> Account Settings -> API Keys)." -ForegroundColor Red
    exit 1
}

function Read-EnvFile([string]$Path) {
    $vars = @{}
    if (-not (Test-Path $Path)) { return $vars }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '^([^=]+)=(.*)$') { return }
        $vars[$Matches[1].Trim()] = $Matches[2].Trim()
    }
    return $vars
}

function Invoke-Render([string]$Method, [string]$Path, $Body = $null) {
    $headers = @{
        Authorization  = "Bearer $env:RENDER_API_KEY"
        Accept         = "application/json"
        "Content-Type" = "application/json"
    }
    $uri = "https://api.render.com/v1$Path"
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 12 -Compress
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -Body $json
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
}

function Test-P1Health([string]$Slug) {
    try {
        $h = Invoke-RestMethod -Uri "https://$Slug.onrender.com/health" -TimeoutSec 30
        return ($h.ok -eq $true -and $h.id -eq "p1")
    } catch { return $false }
}

$local = Read-EnvFile $EnvFile
$token = $local["BOT_TOKEN"]
if (-not $token) { throw "BOT_TOKEN missing in $EnvFile" }

$sshKey = $null
if ($local["VICIDIAL_SSH_KEY"]) {
    $sshKey = $local["VICIDIAL_SSH_KEY"] -replace "`r?`n", '\n'
} elseif (Test-Path $SshKeyFile) {
    $sshKey = (Get-Content -Raw $SshKeyFile) -replace "`r?`n", '\n'
}

$envVars = @(
    @{ key = "CLOUD_DEPLOYED"; value = "true" }
    @{ key = "BOT_TOKEN"; value = $token }
    @{ key = "WEBHOOK_HOST"; value = "0.0.0.0" }
    @{ key = "VICIDIAL_SSH_HOST"; value = "206.189.118.204" }
    @{ key = "VICIDIAL_SSH_USER"; value = "root" }
    @{ key = "VICIDIAL_CAMPAIGN"; value = "press1" }
    @{ key = "VICIDIAL_LIST_ID"; value = "101" }
    @{ key = "VICIDIAL_SOUND_NAME"; value = "press1_alice" }
    @{ key = "VICIDIAL_SERVER_IP"; value = "206.189.118.204" }
    @{ key = "VICIDIAL_MAX_CONCURRENT"; value = "0" }
    @{ key = "VICIDIAL_DIALER_CAP"; value = "0" }
    @{ key = "VICIDIAL_CPS"; value = "10" }
    @{ key = "VICIDIAL_TEST_NUMBERS"; value = "447769799593" }
    @{ key = "PRESS1_OWNER_TEST_NUMBER"; value = "447769799593" }
    @{ key = "BITCALL_SIP_USER"; value = "f-features896" }
    @{ key = "BITCALL_SIP_REALM"; value = "gateway.bitcall.io" }
    @{ key = "DASH_API_SECRET"; value = "dolphin-p1-x7k9m2q4w8" }
    @{ key = "DASH_SUBSCRIPTION_KEYS"; value = "DS-DEMO-2026-KEY1,DS-ADMIN-2026-R8K4N2" }
)
if ($local["TELEGRAM_ALLOWED_IDS"]) {
    $envVars += @{ key = "TELEGRAM_ALLOWED_IDS"; value = $local["TELEGRAM_ALLOWED_IDS"] }
}
if ($local["BITCALL_SIP_PASSWORD"]) {
    $envVars += @{ key = "BITCALL_SIP_PASSWORD"; value = $local["BITCALL_SIP_PASSWORD"] }
}
if ($sshKey) {
    $envVars += @{ key = "VICIDIAL_SSH_KEY"; value = $sshKey }
}

$page = Invoke-Render GET "/services?limit=100"
$p1Services = @()
foreach ($row in $page) {
    $s = $row.service
    if (-not $s) { $s = $row }
    $slug = [string]$s.slug
    $name = [string]$s.name
    if ($slug -match '^p1-' -or $name -match 'p1') {
        $p1Services += $s
    }
}

if (-not $p1Services.Count) {
    Write-Host "No P1 services found on Render." -ForegroundColor Red
    exit 1
}

$healthy = @($p1Services | Where-Object { Test-P1Health $_.slug })
$broken = @($p1Services | Where-Object { -not (Test-P1Health $_.slug) })

Write-Host "P1 services found: $($p1Services.Count)  healthy: $($healthy.Count)  broken: $($broken.Count)"

if ($healthy.Count -ge 1 -and $broken.Count -ge 1) {
    Write-Host "Suspending broken duplicate(s) — keep using https://$($healthy[0].slug).onrender.com" -ForegroundColor Yellow
    foreach ($s in $broken) {
        Write-Host "  Suspending $($s.slug) ($($s.id)) ..."
        Invoke-Render POST "/services/$($s.id)/suspend" @{} | Out-Null
    }
    $target = $healthy[0]
} elseif ($healthy.Count -eq 1) {
    $target = $healthy[0]
    Write-Host "Healthy P1 already running: https://$($target.slug).onrender.com" -ForegroundColor Green
} elseif ($broken.Count -ge 1) {
    $target = $broken[0]
    Write-Host "No healthy P1 — fixing $($target.slug) ($($target.id)) ..." -ForegroundColor Yellow
} else {
    $target = $p1Services[0]
}

$base = "https://$($target.slug).onrender.com"
$webhook = "$base/telegram/webhook"
$sync = $envVars + @(@{ key = "TELEGRAM_WEBHOOK_URL"; value = $webhook })

Write-Host "Syncing env vars to $($target.slug) ..."
Invoke-Render PUT "/services/$($target.id)/env-vars" $sync | Out-Null

Write-Host "Redeploying $($target.slug) ..."
Invoke-Render POST "/services/$($target.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null

Write-Host "Waiting for $base/health ..."
$deadline = (Get-Date).AddMinutes(8)
while ((Get-Date) -lt $deadline) {
    if (Test-P1Health $target.slug) {
        Write-Host "P1 is ready: $base" -ForegroundColor Green
        Invoke-RestMethod "$base/health" | ConvertTo-Json -Compress | Write-Host
        exit 0
    }
    Start-Sleep -Seconds 15
}

Write-Host "Deploy triggered — check Render dashboard. URL: $base" -ForegroundColor Yellow
exit 0
