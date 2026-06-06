# Create Desktop + Start Menu shortcuts for TinyReadAloud (dev / source install).
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))).Path
$PythonW = Join-Path $Root "venv\Scripts\pythonw.exe"
$RunPy = Join-Path $Root "run.py"
$IconIco = Join-Path $Root "assets\shortcut.ico"

if (-not (Test-Path $PythonW)) {
    Write-Error "pythonw not found at $PythonW. Create venv and install deps first."
}
if (-not (Test-Path $RunPy)) {
    Write-Error "run.py not found at $RunPy"
}
if (-not (Test-Path $IconIco)) {
    Write-Host "Generating icons first..."
    & (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "generate_icon.py")
}
$IconIco = (Resolve-Path $IconIco).Path

$WshShell = New-Object -ComObject WScript.Shell

function New-TinyShortcut($LinkPath) {
    if (Test-Path $LinkPath) { Remove-Item $LinkPath -Force }
    $sc = $WshShell.CreateShortcut($LinkPath)
    # Target pythonw directly (not wscript) so Windows Search uses our .ico
    $sc.TargetPath = $PythonW
    $sc.Arguments = "`"$RunPy`""
    $sc.WorkingDirectory = $Root
    $sc.IconLocation = "$IconIco,0"
    $sc.Description = "TinyReadAloud - select text, OCR, listen aloud"
    $sc.Save()
    Write-Host "Created $LinkPath"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$StartMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
New-TinyShortcut (Join-Path $Desktop "TinyReadAloud.lnk")
New-TinyShortcut (Join-Path $StartMenu "TinyReadAloud.lnk")

Write-Host ""
Write-Host "Done. Shortcuts use assets\shortcut.ico (absolute path)."
Write-Host "If Start search still shows the old icon: close TinyReadAloud, run this script again,"
Write-Host "then sign out/in or restart Explorer (icon cache)."
