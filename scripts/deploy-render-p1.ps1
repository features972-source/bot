# Create or update P1 Press-1 bot on Render (separate service — does not touch Q1/Q2/Credo).
#
# Usage:
#   $env:RENDER_API_KEY = "rnd_..."
#   powershell -File scripts/deploy-render-p1.ps1 -BotToken "YOUR_TOKEN"
#
# Optional: copy .env.press1.example -> .env.press1 for VICIDIAL_SSH_KEY and allowed IDs.

param(
    [Parameter(Mandatory = $true)]
    [string]$BotToken,
    [string]$ServiceName = "p1-telegram-bot",
    [string]$Region = "",
    [string]$Repo = "https://github.com/features972-source/bot",
    [string]$Branch = "main",
    [string]$Root = "",
    [string]$EnvFile = "",
    [string]$SshKeyPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Resolve-Path (Join-Path $PSScriptRoot "..") }
if (-not $EnvFile) { $EnvFile = Join-Path $Root ".env.press1" }
if (-not $env:RENDER_API_KEY) { throw "Set RENDER_API_KEY (Render dashboard -> Account Settings -> API Keys)." }

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
    if (-not (Test-Path $Path)) { return $vars }
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
            if ($h.ok -eq $true -and $h.id -eq "p1") { return $h }
            if ($h.ok -eq $true) { Write-Host "  live but id=$($h.id) (expected p1)" }
        } catch {
            Write-Host "  $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 20
    }
    throw "Timed out waiting for $url"
}

function Get-SshKeyForRender {
    param([hashtable]$Local)
    if ($Local["VICIDIAL_SSH_KEY"]) {
        $k = $Local["VICIDIAL_SSH_KEY"]
        if ($k -notmatch '\\n') { return ($k -replace "`r?`n", '\n') }
        return $k
    }
    if ($SshKeyPath -and (Test-Path $SshKeyPath)) {
        $raw = Get-Content -Raw $SshKeyPath
        return ($raw -replace "`r?`n", '\n')
    }
    $wslKey = "\\wsl.localhost\Ubuntu\root\.ssh\do_id"
    if (Test-Path $wslKey) {
        $raw = Get-Content -Raw $wslKey
        return ($raw -replace "`r?`n", '\n')
    }
    return $null
}

Write-Host "=== Deploy P1 Press-1 Bot to Render ===" -ForegroundColor Cyan

$services = Get-AllServices
$q1 = $services | Where-Object {
    $_.slug -match "q1|call-manager" -and $_.slug -notmatch "q2|australia|credo|p1"
} | Select-Object -First 1
if (-not $q1) {
    $q1 = $services | Where-Object { $_.name -match "Q1" } | Select-Object -First 1
}
if (-not $q1) { throw "Could not find Q1 service to copy owner/region from." }

if (-not $Region) { $Region = $q1.serviceDetails.region }
$ownerId = $q1.ownerId
Write-Host "Q1 reference: $($q1.slug) region=$Region owner=$ownerId"

$p1 = $services | Where-Object {
    $_.name -eq $ServiceName -or $_.slug -eq $ServiceName -or $_.slug -match "^p1-"
} | Select-Object -First 1

$local = Read-EnvFile $EnvFile
$sshKey = Get-SshKeyForRender $local

$envVars = @(
    @{ key = "CLOUD_DEPLOYED"; value = "true" }
    @{ key = "BOT_TOKEN"; value = $BotToken }
    @{ key = "WEBHOOK_HOST"; value = "0.0.0.0" }
    @{ key = "VICIDIAL_SSH_HOST"; value = $(if ($local["VICIDIAL_SSH_HOST"]) { $local["VICIDIAL_SSH_HOST"] } else { "206.189.118.204" }) }
    @{ key = "VICIDIAL_SSH_USER"; value = $(if ($local["VICIDIAL_SSH_USER"]) { $local["VICIDIAL_SSH_USER"] } else { "root" }) }
    @{ key = "VICIDIAL_CAMPAIGN"; value = "press1" }
    @{ key = "VICIDIAL_LIST_ID"; value = "101" }
    @{ key = "VICIDIAL_SOUND_NAME"; value = "press1_alice" }
    @{ key = "VICIDIAL_SERVER_IP"; value = "206.189.118.204" }
    @{ key = "VICIDIAL_MAX_CONCURRENT"; value = "0" }
    @{ key = "VICIDIAL_DIALER_CAP"; value = "0" }
    @{ key = "VICIDIAL_CALL_GAP_SEC"; value = "0.1" }
    @{ key = "VICIDIAL_CPS"; value = "10" }
    @{ key = "VICIDIAL_TEST_NUMBERS"; value = "447769799593" }
    @{ key = "PRESS1_OWNER_TEST_NUMBER"; value = "447769799593" }
)

if ($local["TELEGRAM_ALLOWED_IDS"]) {
    $envVars += @{ key = "TELEGRAM_ALLOWED_IDS"; value = $local["TELEGRAM_ALLOWED_IDS"] }
}
if ($sshKey) {
    $envVars += @{ key = "VICIDIAL_SSH_KEY"; value = $sshKey }
    Write-Host "VICIDIAL_SSH_KEY loaded for server access."
} else {
    Write-Warning "VICIDIAL_SSH_KEY not set — add SSH key in Render env after deploy or create .env.press1"
}

$dockerDetails = @{
    dockerfilePath = "./Dockerfile"
    dockerCommand  = "python -u press1_cloud.py"
}
$rootDir = "p1-telegram-bot"

if (-not $p1) {
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
            rootDir         = $rootDir
            envSpecificDetails = $dockerDetails
        }
    }
    $created = Invoke-RenderApi POST "/services" $createBody
    $p1 = if ($created.service) { $created.service } else { $created }
    Write-Host "Created $($p1.slug) id=$($p1.id)"
} else {
    Write-Host "Found existing $($p1.slug) ($($p1.id)) ..."
    $patchBody = @{
        serviceDetails = @{
            rootDir            = $rootDir
            envSpecificDetails = $dockerDetails
        }
    }
    try {
        Invoke-RenderApi PATCH "/services/$($p1.id)" $patchBody | Out-Null
    } catch {
        Write-Warning "Could not patch dockerfile — set Dockerfile.press1 manually in Render dashboard."
    }
}

$baseUrl = "https://$($p1.slug).onrender.com"
Write-Host "Syncing env vars ..."
Invoke-RenderApi PUT "/services/$($p1.id)/env-vars" $envVars | Out-Null

Write-Host "Triggering deploy ..."
Invoke-RenderApi POST "/services/$($p1.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null

try {
    $health = Wait-ForHealth $baseUrl
    Write-Host "P1 bot is live: $baseUrl" -ForegroundColor Green
    $health | ConvertTo-Json -Compress | Write-Host
} catch {
    Write-Warning $_.Exception.Message
    Write-Host "Deploy triggered — check Render dashboard. URL will be: $baseUrl"
}

Write-Host ""
Write-Host "Done. P1 bot URL: $baseUrl" -ForegroundColor Green
Write-Host "Open Telegram -> /start -> send MP3 or numbers -> /run or /testcall"
