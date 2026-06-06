# Local pre-release verification (run before merging PR)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating venv..."
    py -3.12 -m venv venv
    $py = Join-Path $Root "venv\Scripts\python.exe"
}

Write-Host "Installing dependencies..."
& $py -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pip install skipped (app may be running and locking DLLs)"
}

Write-Host "Running automated tests..."
& $py -m tests.test_rex_features
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Running health check..."
& $py -c "import bootstrap; bootstrap.configure_runtime(); from reliability import run_health_check; r=run_health_check(); exit(0 if r.ok else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Health check reported issues - see log above"
    exit 1
}

Write-Host "Running launch smoke test (starts app, verifies log, stops)..."
& $py -m tests.test_launch
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "VERIFY OK - ready to ship"
