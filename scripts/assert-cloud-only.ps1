# Exit unless explicitly allowed — bot runs on Render only.
if ($env:ALLOW_LOCAL_RUN -eq "true" -or $env:ALLOW_LOCAL_RUN -eq "1") {
    return
}
Write-Host ""
Write-Host "Local bot runs are DISABLED." -ForegroundColor Red
Write-Host "This bot runs 24/7 on Render only (bot-josl.onrender.com)." -ForegroundColor Yellow
Write-Host "Set ALLOW_LOCAL_RUN=true only if you are developing locally on purpose." -ForegroundColor DarkGray
Write-Host ""
exit 1
