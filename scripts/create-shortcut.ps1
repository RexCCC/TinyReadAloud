# Create Desktop + Start Menu shortcuts for TinyReadAloud (dev / source install).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LaunchVbs = Join-Path $Root "launch.vbs"
$IconIco = Join-Path $Root "assets\shortcut.ico"

if (-not (Test-Path $LaunchVbs)) {
    Write-Error "launch.vbs not found at $LaunchVbs"
}
if (-not (Test-Path $IconIco)) {
    Write-Host "Generating icons first..."
    & (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "generate_icon.py")
}

$WshShell = New-Object -ComObject WScript.Shell

function New-TinyShortcut($LinkPath) {
    $sc = $WshShell.CreateShortcut($LinkPath)
    $sc.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $sc.Arguments = "`"$LaunchVbs`""
    $sc.WorkingDirectory = $Root
    $sc.IconLocation = "$IconIco,0"
    $sc.Description = "TinyReadAloud — select text, OCR, listen aloud"
    $sc.Save()
    Write-Host "Created $LinkPath"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
New-TinyShortcut (Join-Path $Desktop "TinyReadAloud.lnk")
New-TinyShortcut (Join-Path $StartMenu "TinyReadAloud.lnk")

Write-Host "Done. Shortcuts use assets\shortcut.ico and launch.vbs (no console window)."
