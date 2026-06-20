# Create or update Q2 Call Manager on Render (same repo as Q1, separate service + disk).
# Requires: RENDER_API_KEY and local .env.bot2 with Q2 secrets.
#
# Usage:
#   $env:RENDER_API_KEY = "rnd_..."
#   powershell -File scripts/deploy-render-q2.ps1

param(
    [string]$ServiceName = "q2-telegram-bot",
    [string]$Region = "",
    [string]$Repo = "https://github.com/features972-source/bot",
    [string]$Branch = "main",
    [string]$Root = "",
    [string]$EnvFile = "",
    [switch]$RestoreDb
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Resolve-Path (Join-Path $PSScriptRoot "..") }
if (-not $EnvFile) { $EnvFile = Join-Path $Root ".env.bot2" }
if (-not $env:RENDER_API_KEY) { throw "Set RENDER_API_KEY (Render dashboard -> Account Settings -> API Keys)." }
if (-not (Test-Path $EnvFile)) { throw "Missing $EnvFile" }

$Headers = @{
    Accept         = "application/json"
    "Content-Type" = "application/json"
    Authorization  = "Bearer $env:RENDER_API_KEY"
}
$Api = "https://api.render.com/v1"

function Invoke-RenderApi {
    param([string]$Method, [string]$Path, $Body = $null)
    $uri = "$Api$Path"
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 12 -Compress
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $Headers -Body $json
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $Headers
}

function Get-AllServices {
    $items = @()
    $page = Invoke-RenderApi GET "/services?limit=100"
    foreach ($row in $page) {
        if ($row.service) { $items += $row.service }
        elseif ($row.id) { $items += $row }
    }
    return $items
}

function Read-EnvFile([string]$Path) {
    $vars = @{}
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '^([^=]+)=(.*)$') { return }
        $vars[$Matches[1].Trim()] = $Matches[2].Trim()
    }
    return $vars
}

function Wait-ForHealth([string]$BaseUrl, [int]$Minutes = 25) {
    $url = "$($BaseUrl.TrimEnd('/'))/health"
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ((Get-Date) -lt $deadline) {
        Write-Host "Checking $url ..."
        try {
            $h = Invoke-RestMethod -Uri $url -TimeoutSec 45
            if ($h.ok -eq $true -and $h.id -eq "q2") { return $h }
            if ($h.ok -eq $true) { Write-Host "  live but id=$($h.id) (expected q2)" }
        } catch {
            Write-Host "  $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }
    throw "Timed out waiting for $url"
}

Write-Host "=== Deploy Q2 Call Manager to Render ===" -ForegroundColor Cyan

$services = Get-AllServices
$q1 = $services | Where-Object {
    $_.slug -match "q1|call-manager" -and $_.slug -notmatch "q2|australia"
} | Select-Object -First 1
if (-not $q1) {
    $q1 = $services | Where-Object { $_.name -match "Q1" } | Select-Object -First 1
}
if (-not $q1) { throw "Could not find Q1 service to copy owner/region from." }

if (-not $Region) { $Region = $q1.serviceDetails.region }
$ownerId = $q1.ownerId
Write-Host "Q1 reference: $($q1.slug) region=$Region owner=$ownerId"

$q2 = $services | Where-Object { $_.name -eq $ServiceName -or $_.slug -eq $ServiceName } | Select-Object -First 1

$local = Read-EnvFile $EnvFile
$skip = @("PORT", "RENDER", "RENDER_EXTERNAL_URL", "RENDER_SERVICE_ID", "WEBHOOK_PORT")
$envVars = @()
foreach ($key in ($local.Keys | Sort-Object)) {
    if ($key -in $skip) { continue }
    if ([string]::IsNullOrWhiteSpace($local[$key])) { continue }
    $envVars += @{ key = $key; value = $local[$key] }
}

$required = @{
    CLOUD_DEPLOYED         = "true"
    DATA_DIR               = "/data"
    DATABASE_PATH          = "/data/links-bot2.db"
    PAYMENTS_ONEDRIVE_PATH = "/data/exports/q2.xlsx"
    PAYMENTS_ONEDRIVE_WORKSHEET = "Sheet1"
    BOT_INSTANCE_ID        = "q2"
    BOT_DISPLAY_NAME       = "Q2 Call Manager"
    WEBHOOK_HOST           = "0.0.0.0"
    STATS_TIMEZONE         = "Europe/London"
    READY_CHECK_ENABLED    = "true"
    READY_CHECK_HOUR       = "9"
    TRANSCRIPT_ENABLED     = "false"
}
foreach ($key in $required.Keys) {
    $envVars = @($envVars | Where-Object { $_.key -ne $key })
    $envVars += @{ key = $key; value = $required[$key] }
}

if (-not $q2) {
    Write-Host "Creating service $ServiceName in $Region ..."
    $createBody = @{
        type       = "web_service"
        name       = $ServiceName
        ownerId    = $ownerId
        repo       = $Repo
        branch     = $Branch
        autoDeploy = "yes"
        envVars    = $envVars
        serviceDetails = @{
            runtime         = "docker"
            plan            = "starter"
            region          = $Region
            healthCheckPath = "/health"
            disk = @{
                name      = "q2-bot-data"
                mountPath = "/data"
                sizeGB    = 1
            }
            envSpecificDetails = @{
                dockerfilePath = "./Dockerfile"
            }
        }
    }
    $created = Invoke-RenderApi POST "/services" $createBody
    $q2 = if ($created.service) { $created.service } else { $created }
    Write-Host "Created $($q2.slug) id=$($q2.id)"
} else {
    Write-Host "Found existing $($q2.slug) ($($q2.id)) ..."
}

$baseUrl = "https://$($q2.slug).onrender.com"
$envVars = @($envVars | Where-Object { $_.key -ne "LISTEN_PUBLIC_URL" })
$envVars += @{ key = "LISTEN_PUBLIC_URL"; value = $baseUrl }
Write-Host "Syncing $($envVars.Count) env vars (LISTEN_PUBLIC_URL=$baseUrl) ..."
Invoke-RenderApi PUT "/services/$($q2.id)/env-vars" $envVars | Out-Null

Write-Host "Triggering deploy ..."
Invoke-RenderApi POST "/services/$($q2.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null

$health = Wait-ForHealth $baseUrl
Write-Host "Q2 is live: $baseUrl" -ForegroundColor Green
$health | ConvertTo-Json -Compress | Write-Host

if ($RestoreDb) {
    $dbPath = Join-Path $Root "links-bot2.db"
    if (-not (Test-Path $dbPath)) { throw "Missing $dbPath for -RestoreDb" }
    $secret = $local["WEBHOOK_SECRET"]
    if (-not $secret) { throw "WEBHOOK_SECRET missing in $EnvFile" }
    Write-Host "Restoring $dbPath ..."
    $restore = curl.exe -s -X POST "$baseUrl/admin/restore-db?secret=$secret" -F "file=@$dbPath"
    Write-Host $restore
    $health = Invoke-RestMethod -Uri "$baseUrl/health"
    Write-Host "After restore: payments_logged=$($health.payments_logged)"
}

Write-Host ""
Write-Host "Done. Q2 URL: $baseUrl" -ForegroundColor Green
Write-Host "In the Q2 group run /setnotify and /setnotifypayments once if needed."
