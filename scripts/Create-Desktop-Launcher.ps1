# Shortcut on desktop to run the Python launcher (no EXE build needed).
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Launcher = Join-Path $Root "scripts\bot_launcher.py"
$Desktop = [Environment]::GetFolderPath("Desktop")
$Shortcut = Join-Path $Desktop "Q1 Bot Launcher.lnk"

if (-not (Test-Path $Python)) {
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
}

$Wsh = New-Object -ComObject WScript.Shell
$Link = $Wsh.CreateShortcut($Shortcut)
$Link.TargetPath = $Python
$Link.Arguments = "`"$Launcher`""
$Link.WorkingDirectory = $Root.Path
$Link.IconLocation = "$Python,0"
$Link.Description = "Start Q1 UK, Q2, Q1 Australia bots and link mailer phone"
$Link.Save()

Write-Host "Desktop shortcut created: $Shortcut"
