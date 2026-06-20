# Push full env (including secrets from local env file) to a Render service.
param(
    [Parameter(Mandatory = $true)][string]$ServiceId,
    [ValidateSet("q1", "q2")]
    [string]$Profile = "q1",
    [string]$PublicUrl = "",
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
if (-not $EnvFile) {
    $EnvFile = if ($Profile -eq "q2") {
        Join-Path $Root ".env.bot2"
    } else {
        Join-Path $Root ".env"
    }
}
if (-not $PublicUrl) {
    $PublicUrl = if ($Profile -eq "q2") {
        "https://q2-telegram-bot.onrender.com"
    } else {
        "https://q1-call-manager-eu.onrender.com"
    }
}
if (-not $env:RENDER_API_KEY) { throw "Set RENDER_API_KEY" }

$Headers = @{
    Authorization  = "Bearer $env:RENDER_API_KEY"
    Accept         = "application/json"
    "Content-Type" = "application/json"
}

$vars = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '^([^=]+)=(.*)$') { return }
    $vars[$Matches[1].Trim()] = $Matches[2].Trim()
}

$vars["CLOUD_DEPLOYED"] = "true"
$vars["DATA_DIR"] = "/data"
if ($Profile -eq "q2") {
    $vars["DATABASE_PATH"] = "/data/links-bot2.db"
    $vars["PAYMENTS_ONEDRIVE_PATH"] = "/data/exports/q2.xlsx"
    $vars["BOT_INSTANCE_ID"] = "q2"
    $vars["BOT_DISPLAY_NAME"] = "Q2 Call Manager"
} else {
    $vars["DATABASE_PATH"] = "/data/links.db"
    $vars["PAYMENTS_ONEDRIVE_PATH"] = "/data/exports/q1.xlsx"
    $vars["BOT_INSTANCE_ID"] = "q1"
}
$vars["WEBHOOK_HOST"] = "0.0.0.0"
$vars["LISTEN_PUBLIC_URL"] = $PublicUrl.TrimEnd("/")
$vars.Remove("WEBHOOK_PORT")

$skip = @("PORT", "RENDER", "RENDER_EXTERNAL_URL", "RENDER_SERVICE_ID")
$payload = @()
foreach ($key in ($vars.Keys | Sort-Object)) {
    if ($key -in $skip) { continue }
  if ([string]::IsNullOrWhiteSpace($vars[$key])) { continue }
    $payload += @{ key = $key; value = $vars[$key] }
}

Write-Host "Updating $($payload.Count) env vars on $ServiceId ..."
Invoke-RestMethod -Method PUT -Uri "https://api.render.com/v1/services/$ServiceId/env-vars" -Headers $Headers -Body ($payload | ConvertTo-Json -Depth 4 -Compress) | Out-Null
Write-Host "Env updated."
