# Resume paused Render migration (no create - reuse q1-call-manager-eu).
param(
    [string]$NewSlug = "q1-call-manager-eu",
    [string]$OldSlug = "bot-josl",
    [string]$Root = "",
    [switch]$SuspendOld
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Resolve-Path (Join-Path $PSScriptRoot "..") }
if (-not $env:RENDER_API_KEY) { throw "Set RENDER_API_KEY in the environment first." }

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

function Get-ServiceBySlug([string]$Slug) {
    $page = Invoke-RenderApi GET "/services?limit=100"
    foreach ($row in $page) {
        $s = if ($row.service) { $row.service } else { $row }
        if ($s.slug -eq $Slug -or $s.name -eq $Slug) { return $s }
    }
    return $null
}

function Wait-ForHealth([string]$Slug, [int]$Minutes = 30) {
    $url = "https://$Slug.onrender.com/health"
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ((Get-Date) -lt $deadline) {
        Write-Host "Checking $url ..."
        try {
            $h = Invoke-RestMethod -Uri $url -TimeoutSec 45
            if ($h.ok -eq $true) { return $h }
        } catch {
            Write-Host "  $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 25
    }
    throw "Timed out waiting for $url"
}

$old = Get-ServiceBySlug $OldSlug
$new = Get-ServiceBySlug $NewSlug
if (-not $old) { throw "Old service not found: $OldSlug" }
if (-not $new) { throw "New service not found: $NewSlug" }

Write-Host "Old: $($old.slug) ($($old.serviceDetails.region))"
Write-Host "New: $($new.slug) ($($new.serviceDetails.region))"

$deploys = Invoke-RenderApi GET "/services/$($new.id)/deploys?limit=1"
$latest = if ($deploys[0].deploy) { $deploys[0].deploy } else { $deploys[0] }
Write-Host "Latest new-service deploy: $($latest.status)"

if ($latest.status -notin @("live", "build_in_progress", "update_in_progress")) {
    Write-Host "Triggering new deploy on $($new.slug) ..."
    Invoke-RenderApi POST "/services/$($new.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null
}

$health = Wait-ForHealth $NewSlug
Write-Host "New service is live. payments_logged=$($health.payments_logged)"

$envRows = Invoke-RenderApi GET "/services/$($old.id)/env-vars"
$secret = $null
foreach ($row in $envRows) {
    $ev = if ($row.envVar) { $row.envVar } else { $row }
    if ($ev.key -eq "WEBHOOK_SECRET") { $secret = $ev.value; break }
}
if (-not $secret) {
    $line = Select-String -Path (Join-Path $Root ".env") -Pattern "^WEBHOOK_SECRET=(.+)$" | Select-Object -First 1
    if ($line) { $secret = $line.Matches.Groups[1].Value.Trim() }
}
if (-not $secret) { throw "WEBHOOK_SECRET not found" }

$dbPath = Join-Path $Root "links.db"
if (-not (Test-Path $dbPath)) { throw "Missing $dbPath" }

$newUrl = "https://$($new.slug).onrender.com"
Write-Host "Restoring database ..."
$restore = curl.exe -s -X POST "$newUrl/admin/restore-db?secret=$secret" -F "file=@$dbPath"
Write-Host $restore

$health = Invoke-RestMethod -Uri "$newUrl/health"
Write-Host "After restore: payments_logged=$($health.payments_logged)"

if ($SuspendOld) {
    Write-Host "Suspending $($old.slug) ..."
    Invoke-RenderApi POST "/services/$($old.id)/suspend" | Out-Null
    Write-Host "Old service suspended."
}

Write-Host "Done. New URL: $newUrl"
