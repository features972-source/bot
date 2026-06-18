# Register 3CX Telegram bot to start at Windows login (current user).
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$StartScript = Join-Path $Root "scripts\start-at-login.ps1"
$TaskName = "3CX Telegram Bot"

if (-not (Test-Path $StartScript)) {
    Write-Error "Missing $StartScript"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`""

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Starts 3cx-telegram-bot and Cloudflare tunnel at login." `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "It runs at login for user: $env:USERNAME"
Write-Host "Logs: $Root\logs\bot.log and cloudflared.log"
Write-Host ""
Write-Host "To test now: powershell -File `"$StartScript`""
Write-Host "To remove:   Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
