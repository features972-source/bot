# Starts 3CX Telegram bot (+ optional Cloudflare tunnel) at Windows login.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BotScript = Join-Path $Root "bot.py"
$EnvFile = Join-Path $Root ".env"
$BotLog = Join-Path $LogDir "bot.log"
$BotErrLog = Join-Path $LogDir "bot-error.log"
$TunnelLog = Join-Path $LogDir "cloudflared.log"
$TunnelErrLog = Join-Path $LogDir "cloudflared-error.log"

function Stop-OldBot {
    $rootEscaped = [regex]::Escape($Root.Path)
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -like "*$rootEscaped*bot.py*" -and
                $_.CommandLine -notlike "*--env-file*"
            } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        $remaining = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -like "*$rootEscaped*bot.py*" -and
                $_.CommandLine -notlike "*--env-file*"
            })
        if ($remaining.Count -eq 0) { break }
    }
    Remove-Item (Join-Path $Root "links.db.bot.lock") -Force -ErrorAction SilentlyContinue
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
        Add-Content $BotLog "$(Get-Date -Format o) cloudflared not found - skip tunnel (update LISTEN_PUBLIC_URL manually)"
        return
    }

    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*localhost:8080*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Start-Process -FilePath $exe `
        -ArgumentList "tunnel", "--url", "http://localhost:8080" `
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
        Add-Content $BotLog "$(Get-Date -Format o) Cloudflare tunnel: $url"
    }
}

Stop-OldBot
Start-CloudflaredTunnel

if (-not (Test-Path $Python)) {
    Add-Content $BotLog "$(Get-Date -Format o) ERROR: venv missing at $Python"
    exit 1
}

Add-Content $BotLog "$(Get-Date -Format o) Starting bot..."
Start-Process -FilePath $Python `
    -ArgumentList $BotScript `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $BotLog `
    -RedirectStandardError $BotErrLog

# Q2 is started manually from the desktop launcher when needed.
