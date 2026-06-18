Unregister-ScheduledTask -TaskName "3CX Telegram Bot" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed startup task (if it existed)."
