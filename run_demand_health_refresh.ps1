# run_demand_health_refresh.ps1
#
# Wrapper invoked by the "OMP Demand Health Refresh" scheduled task (see
# setup_demand_health_refresh_task.ps1). Runs run_demand_health_live.py
# --no-browser so the data refreshes on disk without popping a browser
# window open, and logs start/success/failure with timestamps -- same
# pattern as run_webapp_update.ps1's Write-Log/refresh_log.txt convention.
#
# You generally don't need to run this by hand -- the scheduled task calls
# it automatically. To test it manually:
#   PowerShell -ExecutionPolicy Bypass -File ".\run_demand_health_refresh.ps1"

# Intentionally NOT "Stop" -- python.exe writing anything at all to stderr
# (even a harmless warning, not an actual failure) otherwise gets wrapped
# into a terminating NativeCommandError and kills this script mid-run,
# before the refresh finishes. Real failures are still caught below via
# $LASTEXITCODE, which is the correct way to check a native command's
# actual success/failure regardless of what it wrote to which stream.
$ErrorActionPreference = "Continue"

$RepoDir   = "H:\2025\NewForecastingModel\OMPforecasting5"
$PythonExe = "$RepoDir\.venv\Scripts\python.exe"
$LogFile   = "$RepoDir\demand_health_refresh_log.txt"

Set-Location $RepoDir

function Write-Log {
    param([string]$Level, [string]$Msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts [$Level] $Msg"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

Write-Log "INFO" "============================================================"
Write-Log "INFO" "Demand Health refresh started"

$start = Get-Date
& $PythonExe ".\run_demand_health_live.py" "--no-browser" 2>&1 | ForEach-Object {
    Add-Content -Path $LogFile -Value $_
}
$pyExit = $LASTEXITCODE
$elapsed = [int]((Get-Date) - $start).TotalSeconds

if ($pyExit -ne 0) {
    Write-Log "ERROR" "run_demand_health_live.py failed (exit $pyExit) after ${elapsed}s"
    Write-Log "INFO" "============================================================"
    exit 1
}

Write-Log "INFO" "Demand Health refresh completed in ${elapsed}s"
Write-Log "INFO" "============================================================"
