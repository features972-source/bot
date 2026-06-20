# Migrate Q1 Call Manager to a new Render region (same data).
# Requires: RENDER_API_KEY from https://dashboard.render.com/u/settings/api-keys
#
# Usage:
#   $env:RENDER_API_KEY = "rnd_..."
#   powershell -File scripts/migrate-render-region.ps1 -SuspendOld

param(
    [string]$Region = "frankfurt",
    [string]$NewServiceName = "q1-call-manager-eu",
    [string]$OldServiceSlug = "bot-josl",
    [string]$Repo = "https://github.com/features972-source/bot",
    [string]$Branch = "main",
    [string]$Root = "",
    [switch]$SuspendOld,
    [switch]$DeleteOld
)

$ErrorActionPreference = "Stop"
if (-not $Root) {
    $Root = Resolve-Path (Join-Path $PSScriptRoot "..")
}

$ApiKey = $env:RENDER_API_KEY
if (-not $ApiKey) {
    throw "Set RENDER_API_KEY first (Render Dashboard -> Account Settings -> API Keys)."
}

$Headers = @{
    Accept         = "application/json"
    "Content-Type" = "application/json"
    Authorization  = "Bearer $ApiKey"
}
$Api = "https://api.render.com/v1"

function Invoke-RenderApi {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null
    )
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

function Wait-ForServiceLive {
    param([string]$ServiceId, [int]$TimeoutMinutes = 25)
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline) {
        $svc = Invoke-RenderApi GET "/services/$ServiceId"
        $s = if ($svc.service) { $svc.service } else { $svc }
        $url = "https://$($s.slug).onrender.com/health"
        Write-Host "Waiting for $url ..."
        try {
            $health = Invoke-RestMethod -Uri $url -TimeoutSec 30
            if ($health.ok -eq $true) {
                return $s
            }
        } catch {
            Write-Host "  not ready yet ($($_.Exception.Message))"
        }
        Start-Sleep -Seconds 20
    }
    throw "Timed out waiting for new service health check."
}

Write-Host "=== Q1 Call Manager - Render region migration ===" -ForegroundColor Cyan
Write-Host "Target region: $Region"
Write-Host "New service name: $NewServiceName"
Write-Host ""

$services = Get-AllServices
$old = $services | Where-Object { $_.slug -eq $OldServiceSlug -or $_.name -eq $OldServiceSlug } | Select-Object -First 1
if (-not $old) {
    throw "Could not find old service slug '$OldServiceSlug'. Check Render dashboard."
}

$ownerId = $old.ownerId
Write-Host "Found old service: $($old.name) ($($old.slug)) in region $($old.serviceDetails.region)"

Write-Host "Reading env vars from old service ..."
$envRows = Invoke-RenderApi GET "/services/$($old.id)/env-vars"

$existingNew = $services | Where-Object { $_.slug -eq $NewServiceName -or $_.name -eq $NewServiceName } | Select-Object -First 1
if ($existingNew) {
    Write-Host "Service '$NewServiceName' already exists - reusing it." -ForegroundColor Yellow
    $newService = $existingNew
} else {
    $envVars = @()
    foreach ($row in $envRows) {
        $ev = if ($row.envVar) { $row.envVar } else { $row }
        if (-not $ev.key) { continue }
        if ($ev.key -in @("PORT", "RENDER", "RENDER_EXTERNAL_URL", "RENDER_SERVICE_ID", "RENDER_GIT_BRANCH", "RENDER_GIT_COMMIT")) {
            continue
        }
        $envVars += @{ key = $ev.key; value = $ev.value }
    }

    $required = @{
        CLOUD_DEPLOYED         = "true"
        DATA_DIR               = "/data"
        DATABASE_PATH          = "/data/links.db"
        PAYMENTS_ONEDRIVE_PATH = "/data/exports/q1.xlsx"
        BOT_INSTANCE_ID        = "q1"
        WEBHOOK_HOST           = "0.0.0.0"
    }
    foreach ($key in $required.Keys) {
        $envVars = @($envVars | Where-Object { $_.key -ne $key })
        $envVars += @{ key = $key; value = $required[$key] }
    }

    Write-Host "Creating new web service in $Region ..."
    $createBody = @{
        type       = "web_service"
        name       = $NewServiceName
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
                name      = "q1-bot-data"
                mountPath = "/data"
                sizeGB    = 1
            }
            envSpecificDetails = @{
                dockerfilePath = "./Dockerfile"
            }
        }
    }
    $created = Invoke-RenderApi POST "/services" $createBody
    $newService = if ($created.service) { $created.service } else { $created }
    Write-Host "Created service id $($newService.id) slug $($newService.slug)"
}

$newUrl = "https://$($newService.slug).onrender.com"
Write-Host "Waiting for deploy at $newUrl ..."
$live = Wait-ForServiceLive -ServiceId $newService.id
$newUrl = "https://$($live.slug).onrender.com"

$dbPath = Join-Path $Root "links.db"
if (-not (Test-Path $dbPath)) {
    throw "Missing $dbPath - need local database backup to restore."
}

$secret = $null
foreach ($row in $envRows) {
    $ev = if ($row.envVar) { $row.envVar } else { $row }
    if ($ev.key -eq "WEBHOOK_SECRET") {
        $secret = $ev.value
        break
    }
}
if (-not $secret) {
    $line = Select-String -Path (Join-Path $Root ".env") -Pattern "^WEBHOOK_SECRET=(.+)$" | Select-Object -First 1
    if ($line) { $secret = $line.Matches.Groups[1].Value.Trim() }
}
if (-not $secret) {
    throw "Could not find WEBHOOK_SECRET on old service or in .env"
}

Write-Host "Restoring database to $newUrl ..."
$restoreUrl = "$newUrl/admin/restore-db?secret=$secret"
$restore = curl.exe -s -X POST $restoreUrl -F "file=@$dbPath"
Write-Host $restore

$health = Invoke-RestMethod -Uri "$newUrl/health"
Write-Host ""
Write-Host "New service health:" -ForegroundColor Green
$health | ConvertTo-Json -Compress

if ($health.payments_logged -lt 1) {
    Write-Host "WARNING: payments_logged is low - verify restore succeeded." -ForegroundColor Yellow
}

if ($SuspendOld -or $DeleteOld) {
    Write-Host ""
    Write-Host "Suspending OLD service $($old.slug) so only one bot polls Telegram ..."
    Invoke-RenderApi POST "/services/$($old.id)/suspend" | Out-Null
    Write-Host "Old service suspended." -ForegroundColor Yellow
}

if ($DeleteOld) {
    Write-Host "Deleting OLD service $($old.slug) ..."
    Invoke-RenderApi DELETE "/services/$($old.id)" | Out-Null
    Write-Host "Old service deleted." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "New URL: $newUrl"
Write-Host "Health:  $newUrl/health"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Test /help and /payments in Telegram"
Write-Host "  2. Update Azure OAuth redirect: $newUrl/oauth/msgraph/callback"
Write-Host "  3. Set LISTEN_PUBLIC_URL=$newUrl in local .env"
if (-not $SuspendOld -and -not $DeleteOld) {
    Write-Host "  4. Suspend old service '$($old.slug)' in Render when happy (or re-run with -SuspendOld)"
}
