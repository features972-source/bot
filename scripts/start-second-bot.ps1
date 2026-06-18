# Second bot instance: separate token, database, notify group, webhook port.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvFile = Join-Path $Root ".env.bot2"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BotScript = Join-Path $Root "bot.py"
$BotLog = Join-Path $LogDir "bot2.log"
$BotErrLog = Join-Path $LogDir "bot2-error.log"
$TunnelLog = Join-Path $LogDir "cloudflared-bot2.log"
$TunnelErrLog = Join-Path $LogDir "cloudflared-bot2-error.log"

if (-not (Test-Path $EnvFile)) {
    Write-Host "Missing $EnvFile - copy .env.bot2.example to .env.bot2 and edit it."
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
        Add-Content $BotLog "$(Get-Date -Format o) cloudflared not found - skip bot2 tunnel"
        return
    }

    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*localhost:8081*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Start-Process -FilePath $exe `
        -ArgumentList "tunnel", "--url", "http://localhost:8081" `
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
        Add-Content $BotLog "$(Get-Date -Format o) Cloudflare tunnel (bot2): $url"
    }
}

function Stop-SecondBot {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*3cx-telegram-bot*bot.py*--env-file*.env.bot2*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Remove-Item (Join-Path $Root "links-bot2.db.bot.lock") -Force -ErrorAction SilentlyContinue
}

Stop-SecondBot
Start-CloudflaredTunnel

if (-not (Test-Path $Python)) {
    Add-Content $BotLog "$(Get-Date -Format o) ERROR: venv missing at $Python"
    exit 1
}

Add-Content $BotLog "$(Get-Date -Format o) Starting bot instance 2 (.env.bot2)..."
Start-Process -FilePath $Python `
    -ArgumentList $BotScript, "--env-file", ".env.bot2" `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $BotLog `
    -RedirectStandardError $BotErrLog

Write-Host "Second bot started. Logs: $BotLog"
