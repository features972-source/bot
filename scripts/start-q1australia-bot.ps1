# Q1 Australia bot instance: separate token, database, notify group, webhook port.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvFile = Join-Path $Root ".env.q1australia"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BotScript = Join-Path $Root "bot.py"
$BotLog = Join-Path $LogDir "q1australia.log"
$BotErrLog = Join-Path $LogDir "q1australia-error.log"
$TunnelLog = Join-Path $LogDir "cloudflared-q1australia.log"
$TunnelErrLog = Join-Path $LogDir "cloudflared-q1australia-error.log"
$WebhookPort = 8082

if (-not (Test-Path $EnvFile)) {
    Write-Host "Missing $EnvFile - copy .env.q1australia.example to .env.q1australia and edit it."
    exit 1
}

function Find-Cloudflared {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($path in @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe",
        "$env:ProgramFiles\Cloudflare\cloudflared\cloudflared.exe",
        "$env:USERPROFILE\cloudflared\cloudflared.exe"
    )) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Start-CloudflaredTunnel {
    $exe = Find-Cloudflared
    if (-not $exe) {
        Add-Content $BotLog "$(Get-Date -Format o) cloudflared not found - skip Q1 Australia tunnel"
        return
    }

    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*localhost:$WebhookPort*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Start-Process -FilePath $exe `
        -ArgumentList "tunnel", "--url", "http://localhost:$WebhookPort" `
        -WindowStyle Hidden `
        -RedirectStandardOutput $TunnelLog `
        -RedirectStandardError $TunnelErrLog

    $url = $null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        foreach ($logPath in @($TunnelLog, $TunnelErrLog)) {
            if (-not (Test-Path $logPath)) { continue }
            $match = Select-String -Path $logPath -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -AllMatches |
                Select-Object -Last 1
            if ($match) {
                $url = $match.Matches[0].Value
                break
            }
        }
        if ($url) { break }
    }

    if ($url -and (Test-Path $EnvFile)) {
        $content = Get-Content $EnvFile -Raw
        if ($content -match "LISTEN_PUBLIC_URL=.*") {
            $content = [regex]::Replace($content, "LISTEN_PUBLIC_URL=.*", "LISTEN_PUBLIC_URL=$url")
        } else {
            $content = $content.TrimEnd() + "`nLISTEN_PUBLIC_URL=$url`n"
        }
        Set-Content -Path $EnvFile -Value $content -NoNewline
        Add-Content $BotLog "$(Get-Date -Format o) Cloudflare tunnel (Q1 Australia): $url"
    }
}

function Stop-Q1AustraliaBot {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*3cx-telegram-bot*bot.py*--env-file*.env.q1australia*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Remove-Item (Join-Path $Root "links-q1australia.db.bot.lock") -Force -ErrorAction SilentlyContinue
}

Stop-Q1AustraliaBot
Start-CloudflaredTunnel

if (-not (Test-Path $Python)) {
    Add-Content $BotLog "$(Get-Date -Format o) ERROR: venv missing at $Python"
    exit 1
}

Add-Content $BotLog "$(Get-Date -Format o) Starting Q1 Australia (.env.q1australia)..."
Start-Process -FilePath $Python `
    -ArgumentList $BotScript, "--env-file", ".env.q1australia" `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $BotLog `
    -RedirectStandardError $BotErrLog

Write-Host "Q1 Australia bot started. Logs: $BotLog"
