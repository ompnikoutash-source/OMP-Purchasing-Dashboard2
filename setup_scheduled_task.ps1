# setup_scheduled_task.ps1
#
# Registers (or updates) a Windows Task Scheduler task that runs
# refresh_forecast_data.py every hour to keep the OMP Purchasing
# Dashboard data fresh.
#
# Run this script ONCE from an Administrator PowerShell prompt:
#
#   PowerShell -ExecutionPolicy Bypass -File ".\setup_scheduled_task.ps1"
#
# To change the interval (e.g. every 2 hours):
#   PowerShell -ExecutionPolicy Bypass -File ".\setup_scheduled_task.ps1" -IntervalHours 2
#
# To remove the task:
#   schtasks /delete /tn "OMP Dashboard Hourly Refresh" /f

param(
    [string]$TaskName     = "OMP Dashboard Hourly Refresh",
    [int]   $IntervalHours = 1,
    [string]$PythonExe    = ""      # leave blank to auto-detect
)

$ErrorActionPreference = "Stop"
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$RefreshScript = Join-Path $ScriptDir "refresh_forecast_data.py"

# ── Locate Python ──────────────────────────────────────────────────────────────
if (-not $PythonExe) {
    $candidates = @(
        (Join-Path $ScriptDir ".venv\Scripts\python.exe"),
        (Join-Path $ScriptDir "venv\Scripts\python.exe"),
        (Join-Path $ScriptDir ".env\Scripts\python.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $PythonExe = $c; break }
    }
    if (-not $PythonExe) {
        Write-Error "Could not find python.exe. Pass -PythonExe 'C:\path\to\python.exe'"
        exit 1
    }
}

if (-not (Test-Path $RefreshScript)) {
    Write-Error "refresh_forecast_data.py not found at $RefreshScript"
    exit 1
}

Write-Host ""
Write-Host "Task name   : $TaskName"
Write-Host "Python      : $PythonExe"
Write-Host "Script      : $RefreshScript"
Write-Host "Working dir : $ScriptDir"
Write-Host "Interval    : every $IntervalHours hour(s)"
Write-Host ""

# ── Register via schtasks.exe (fully compatible with PS 5.1) ──────────────────
# /sc hourly /mo N  = repeat every N hours, indefinitely
# /f                = overwrite if task already exists
# /rl HIGHEST       = run with highest privileges available to the user
$tr = "`"$PythonExe`" `"$RefreshScript`""

schtasks /create `
    /tn  $TaskName `
    /tr  $tr `
    /sc  hourly `
    /mo  $IntervalHours `
    /sd  (Get-Date -Format "MM/dd/yyyy") `
    /st  (Get-Date -Format "HH:mm") `
    /rl  HIGHEST `
    /f

if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks failed with exit code $LASTEXITCODE"
    exit 1
}

Write-Host ""
Write-Host "Task registered successfully."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now    : schtasks /run /tn `"$TaskName`""
Write-Host "  Check log  : notepad `"$ScriptDir\refresh_log.txt`""
Write-Host "  View task  : schtasks /query /tn `"$TaskName`" /fo LIST"
Write-Host "  Remove     : schtasks /delete /tn `"$TaskName`" /f"
Write-Host ""
