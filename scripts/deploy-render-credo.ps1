# Create or update Credo Bot on Render (cc commands only — separate service + disk).
# Requires: RENDER_API_KEY and local .env.credo with BOT_TOKEN + ADMIN_CHAT_ID.
#
# Usage:
#   $env:RENDER_API_KEY = "rnd_..."
#   powershell -File scripts/deploy-render-credo.ps1

param(
    [string]$ServiceName = "credo-telegram-bot",
    [string]$Region = "",
    [string]$Repo = "https://github.com/features972-source/bot",
    [string]$Branch = "main",
    [string]$Root = "",
    [string]$EnvFile = "",
    [switch]$RestoreDb
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Resolve-Path (Join-Path $PSScriptRoot "..") }
if (-not $EnvFile) { $EnvFile = Join-Path $Root ".env.credo" }
if (-not $env:RENDER_API_KEY) { throw "Set RENDER_API_KEY (Render dashboard -> Account Settings -> API Keys)." }
if (-not (Test-Path $EnvFile)) { throw "Missing $EnvFile — copy .env.credo.example to .env.credo and fill in BOT_TOKEN." }

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
            if ($h.ok -eq $true -and $h.id -eq "credo") { return $h }
            if ($h.ok -eq $true) { Write-Host "  live but id=$($h.id) (expected credo)" }
        } catch {
            Write-Host "  $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }
    throw "Timed out waiting for $url"
}

Write-Host "=== Deploy Credo Bot to Render ===" -ForegroundColor Cyan

$services = Get-AllServices
$q1 = $services | Where-Object {
    $_.slug -match "q1|call-manager" -and $_.slug -notmatch "q2|australia|credo"
} | Select-Object -First 1
if (-not $q1) {
    $q1 = $services | Where-Object { $_.name -match "Q1" } | Select-Object -First 1
}
if (-not $q1) { throw "Could not find Q1 service to copy owner/region from." }

if (-not $Region) { $Region = $q1.serviceDetails.region }
$ownerId = $q1.ownerId
Write-Host "Q1 reference: $($q1.slug) region=$Region owner=$ownerId"

$credo = $services | Where-Object {
    $_.name -eq $ServiceName -or $_.slug -eq $ServiceName -or $_.slug -match "credo"
} | Select-Object -First 1

$local = Read-EnvFile $EnvFile
$skip = @("PORT", "RENDER", "RENDER_EXTERNAL_URL", "RENDER_SERVICE_ID", "WEBHOOK_PORT")
$envVars = @()
foreach ($key in ($local.Keys | Sort-Object)) {
    if ($key -in $skip) { continue }
    if ([string]::IsNullOrWhiteSpace($local[$key])) { continue }
    $envVars += @{ key = $key; value = $local[$key] }
}

$required = @{
    CLOUD_DEPLOYED      = "true"
    CREDO_ONLY_MODE     = "true"
    DATA_DIR            = "/data"
    DATABASE_PATH       = "/data/links-credo.db"
    BOT_INSTANCE_ID     = "credo"
    BOT_DISPLAY_NAME    = "Credo Bot"
    WEBHOOK_HOST        = "0.0.0.0"
    STATS_TIMEZONE      = "Europe/London"
    READY_CHECK_ENABLED = "false"
}
foreach ($key in $required.Keys) {
    $envVars = @($envVars | Where-Object { $_.key -ne $key })
    $envVars += @{ key = $key; value = $required[$key] }
}

if (-not $credo) {
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
                name      = "credo-bot-data"
                mountPath = "/data"
                sizeGB    = 1
            }
            envSpecificDetails = @{
                dockerfilePath = "./Dockerfile"
                dockerCommand  = "python bot_credo.py"
            }
        }
    }
    $created = Invoke-RenderApi POST "/services" $createBody
    $credo = if ($created.service) { $created.service } else { $created }
    Write-Host "Created $($credo.slug) id=$($credo.id)"
} else {
    Write-Host "Found existing $($credo.slug) ($($credo.id)) ..."
    Write-Host "Updating docker command to python bot_credo.py ..."
    $patchBody = @{
        serviceDetails = @{
            envSpecificDetails = @{
                dockerfilePath = "./Dockerfile"
                dockerCommand  = "python bot_credo.py"
            }
        }
    }
    try {
        Invoke-RenderApi PATCH "/services/$($credo.id)" $patchBody | Out-Null
    } catch {
        Write-Warning "Could not patch dockerCommand — set manually in Render dashboard: python bot_credo.py"
    }
}

$baseUrl = "https://$($credo.slug).onrender.com"
$envVars = @($envVars | Where-Object { $_.key -ne "LISTEN_PUBLIC_URL" })
$envVars += @{ key = "LISTEN_PUBLIC_URL"; value = $baseUrl }
Write-Host "Syncing $($envVars.Count) env vars (LISTEN_PUBLIC_URL=$baseUrl) ..."
Invoke-RenderApi PUT "/services/$($credo.id)/env-vars" $envVars | Out-Null

Write-Host "Triggering deploy ..."
Invoke-RenderApi POST "/services/$($credo.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null

$health = Wait-ForHealth $baseUrl
Write-Host "Credo bot is live: $baseUrl" -ForegroundColor Green
$health | ConvertTo-Json -Compress | Write-Host

if ($RestoreDb) {
    $dbPath = Join-Path $Root "links-credo.db"
    if (-not (Test-Path $dbPath)) { throw "Missing $dbPath for -RestoreDb" }
    $secret = $local["WEBHOOK_SECRET"]
    if (-not $secret) { throw "WEBHOOK_SECRET missing in $EnvFile" }
    Write-Host "Restoring $dbPath ..."
    $restore = curl.exe -s -X POST "$baseUrl/admin/restore-db?secret=$secret" -F "file=@$dbPath"
    Write-Host $restore
}

Write-Host ""
Write-Host "Done. Credo bot URL: $baseUrl" -ForegroundColor Green
Write-Host "DM the bot /start, then /addcredo to add cards."
Write-Host "Every git push to main also auto-deploys this service (same repo)."
