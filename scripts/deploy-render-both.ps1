# Trigger deploy on Q1 and Q2 Render services and verify both are live on the same commit.
# Every push to main auto-deploys both (same GitHub repo) — run this to force redeploy or verify.
#
# Usage:
#   $env:RENDER_API_KEY = "rnd_..."
#   powershell -File scripts/deploy-render-both.ps1          # trigger + wait
#   powershell -File scripts/deploy-render-both.ps1 -VerifyOnly

param(
    [switch]$VerifyOnly,
    [switch]$SkipTrigger,
    [int]$WaitMinutes = 25,
    [string]$Repo = "https://github.com/features972-source/bot",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
if (-not $env:RENDER_API_KEY) {
    throw "Set RENDER_API_KEY (Render dashboard -> Account Settings -> API Keys)."
}

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
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

function Get-BotDeployTargets {
    param($Services)

    $q1 = $Services | Where-Object {
        $_.repo -eq $Repo -and
        $_.branch -eq $Branch -and
        $_.suspended -eq "not_suspended" -and
        ($_.slug -eq "q1-call-manager-eu" -or ($_.slug -match "q1" -and $_.slug -notmatch "q2|australia|botq"))
    } | Select-Object -First 1

    $q2 = $Services | Where-Object {
        $_.repo -eq $Repo -and
        $_.branch -eq $Branch -and
        $_.suspended -eq "not_suspended" -and
        ($_.slug -eq "q2-telegram-bot" -or ($_.slug -match "q2" -and $_.slug -notmatch "botq"))
    } | Select-Object -First 1

    if (-not $q1) { throw "Active Q1 service not found for repo $Repo branch $Branch." }
    if (-not $q2) { throw "Active Q2 service not found for repo $Repo branch $Branch." }
    return @($q1, $q2)
}

function Get-LatestDeploy($ServiceId) {
    $rows = Invoke-RenderApi GET "/services/$ServiceId/deploys?limit=1"
    if (-not $rows -or $rows.Count -eq 0) { return $null }
    $row = $rows[0]
    if ($row.deploy) { return $row.deploy }
    return $row
}

function Wait-ForServiceLive {
    param($Service, [string]$ExpectedInstanceId, [int]$Minutes)

    $baseUrl = $Service.serviceDetails.url
    if (-not $baseUrl) { $baseUrl = "https://$($Service.slug).onrender.com" }
    $url = "$($baseUrl.TrimEnd('/'))/health"
    $deadline = (Get-Date).AddMinutes($Minutes)

    while ((Get-Date) -lt $deadline) {
        Write-Host "  health $url ..."
        try {
            $h = Invoke-RestMethod -Uri $url -TimeoutSec 45
            if ($h.ok -eq $true -and $h.id -eq $ExpectedInstanceId) {
                return @{ health = $h; url = $baseUrl }
            }
            if ($h.ok -eq $true) {
                Write-Host "    live but id=$($h.id) (expected $ExpectedInstanceId)"
            }
        } catch {
            Write-Host "    $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }
    throw "Timed out waiting for $url"
}

Write-Host "=== Q1 + Q2 Render deploy ===" -ForegroundColor Cyan

$localCommit = ""
try {
    Push-Location $Root
    $localCommit = (git rev-parse HEAD).Trim()
    Write-Host "Local commit: $localCommit"
} finally {
    Pop-Location
}

$services = Get-AllServices
$targets = Get-BotDeployTargets -Services $services
$q1 = $targets[0]
$q2 = $targets[1]

Write-Host ""
Write-Host "Q1: $($q1.name) ($($q1.slug)) $($q1.serviceDetails.url)"
Write-Host "Q2: $($q2.name) ($($q2.slug)) $($q2.serviceDetails.url)"

if (-not $VerifyOnly -and -not $SkipTrigger) {
    foreach ($svc in $targets) {
        Write-Host "Triggering deploy $($svc.slug) ..."
        Invoke-RenderApi POST "/services/$($svc.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null
    }
}

Write-Host ""
Write-Host "Waiting for both services ..."
$q1Live = Wait-ForServiceLive -Service $q1 -ExpectedInstanceId "q1" -Minutes $WaitMinutes
$q2Live = Wait-ForServiceLive -Service $q2 -ExpectedInstanceId "q2" -Minutes $WaitMinutes

$q1Deploy = Get-LatestDeploy $q1.id
$q2Deploy = Get-LatestDeploy $q2.id
$q1Commit = if ($q1Deploy.commit) { $q1Deploy.commit.id } else { "" }
$q2Commit = if ($q2Deploy.commit) { $q2Deploy.commit.id } else { "" }

Write-Host ""
Write-Host "Q1 live: $($q1Live.url) commit=$q1Commit status=$($q1Deploy.status)"
Write-Host "Q2 live: $($q2Live.url) commit=$q2Commit status=$($q2Deploy.status)"

if ($localCommit -and $q1Commit -and $q2Commit) {
    if ($q1Commit.StartsWith($localCommit.Substring(0, 7)) -and $q2Commit.StartsWith($localCommit.Substring(0, 7))) {
        Write-Host ""
        Write-Host "Both services match local main ($($localCommit.Substring(0, 7)))." -ForegroundColor Green
    } else {
        Write-Warning "Commit mismatch — local=$localCommit q1=$q1Commit q2=$q2Commit"
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
