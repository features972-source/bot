# One-time: stop laptop bot, remove startup task and desktop launcher.
$ErrorActionPreference = "Continue"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

Write-Host "Disabling local bot on this PC..." -ForegroundColor Cyan

# Remove Windows login scheduled task
Unregister-ScheduledTask -TaskName "3CX Telegram Bot" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "  Removed scheduled task '3CX Telegram Bot' (if it existed)."

# Stop any running bot processes from this repo
$rootEscaped = [regex]::Escape($Root.Path)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like "*$rootEscaped*bot.py*" -or
        $_.CommandLine -like "*$rootEscaped*bot_launcher.py*"
    } |
    ForEach-Object {
        Write-Host "  Stopping python PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$rootEscaped*bot_launcher.py*" } |
    ForEach-Object {
        Write-Host "  Stopping pythonw PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*localhost:8080*" -or $_.CommandLine -like "*localhost:8081*" } |
    ForEach-Object {
        Write-Host "  Stopping cloudflared PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

foreach ($lock in @("links.db.bot.lock", "links-bot2.db.bot.lock", "links-q1australia.db.bot.lock")) {
    Remove-Item (Join-Path $Root $lock) -Force -ErrorAction SilentlyContinue
}

# Remove desktop launcher shortcut
$Desktop = [Environment]::GetFolderPath("Desktop")
foreach ($name in @("Q1 Bot Launcher.lnk", "3CX Telegram Bot.lnk")) {
    $shortcut = Join-Path $Desktop $name
    if (Test-Path $shortcut) {
        Remove-Item $shortcut -Force
        Write-Host "  Removed desktop shortcut: $shortcut"
    }
}

Write-Host ""
Write-Host "Done. Bot only runs on Render now." -ForegroundColor Green
Write-Host "Starting bot.py on this PC will exit with an error (by design)." -ForegroundColor DarkGray
