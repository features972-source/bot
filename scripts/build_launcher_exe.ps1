# Build desktop launcher EXE (run once from PowerShell).
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Desktop = [Environment]::GetFolderPath("Desktop")

if (-not (Test-Path $Python)) {
    Write-Error "venv missing. Run: python -m venv .venv && pip install -r requirements.txt pyinstaller"
    exit 1
}

& $Python -m pip install pyinstaller -q
& $Python -m PyInstaller `
    --onefile `
    --windowed `
    --name "Q1 Bot Launcher" `
    --distpath (Join-Path $Root "dist") `
    --workpath (Join-Path $Root "build\launcher") `
    --specpath (Join-Path $Root "build\launcher") `
    (Join-Path $Root "scripts\bot_launcher.py")

$Exe = Join-Path $Root "dist\Q1 Bot Launcher.exe"
if (Test-Path $Exe) {
    Copy-Item $Exe (Join-Path $Desktop "Q1 Bot Launcher.exe") -Force
    Write-Host "Built: $Exe"
    Write-Host "Copied to desktop: $(Join-Path $Desktop 'Q1 Bot Launcher.exe')"
} else {
    Write-Error "Build failed — EXE not found."
}
