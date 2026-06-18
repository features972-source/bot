# Wipe local bot database copies on this PC (run after /panic on Render).
$ErrorActionPreference = "Continue"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

Write-Host "Wiping local bot data on this PC..." -ForegroundColor Red
Write-Host "Press Ctrl+C within 5 seconds to cancel."
Start-Sleep -Seconds 5

$removed = @()

foreach ($name in @(
        "links.db",
        "links-bot2.db",
        "links-q1australia.db",
        "links.db-wal",
        "links.db-shm",
        "links-bot2.db-wal",
        "links-bot2.db-shm",
        "links-q1australia.db-wal",
        "links-q1australia.db-shm",
        "links.db.bot.lock",
        "links-bot2.db.bot.lock",
        "links-q1australia.db.bot.lock"
    )) {
    $path = Join-Path $Root $name
    if (Test-Path $path) {
        Remove-Item $path -Force
        $removed += $path
        Write-Host "  Removed $path"
    }
}

foreach ($dirName in @("data", "backups")) {
    $dir = Join-Path $Root $dirName
    if (Test-Path $dir) {
        Get-ChildItem $dir -Recurse -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        $removed += "$dir\*"
        Write-Host "  Cleared $dir"
    }
}

$dataBackups = Join-Path $Root "data\backups"
if (Test-Path $dataBackups) {
    Remove-Item $dataBackups -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  Cleared $dataBackups"
}

$dataExports = Join-Path $Root "data\exports"
if (Test-Path $dataExports) {
    Remove-Item $dataExports -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  Cleared $dataExports"
}

Write-Host ""
if ($removed.Count -eq 0) {
    Write-Host "No local bot database files found under $Root" -ForegroundColor Yellow
} else {
    Write-Host "Local wipe complete." -ForegroundColor Green
}

Write-Host "Cloud data is only wiped when you confirm /panic on Telegram." -ForegroundColor DarkGray
