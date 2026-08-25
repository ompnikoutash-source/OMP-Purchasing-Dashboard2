# setup_forecast_reconciliation_task.ps1
#
# Creates (or replaces) a Windows scheduled task that runs the forecast
# accuracy reconciliation pipeline (extractor -> reconciler -> report)
# weekly, as the current logged-in user.
#
# This is a SEPARATE task from "OMP Dashboard Refresh" (run_webapp_update.ps1,
# daily at 11:00/14:00) rather than piggybacked onto it: a reconciler hang
# (a real risk given agents/forecast_history_db.py's documented SQLite/
# network-share locking fragility) should never be able to delay the
# twice-daily dashboard refresh the business depends on, and a weekly
# trigger avoids needing a de-dup guard for a script that runs twice a day.
#
# Does NOT require Administrator rights -- the task is owned by the current
# user account and is fully visible in Task Scheduler without elevation.
#
# Usage (run once from any PowerShell prompt -- no "Run as Administrator"):
#   PowerShell -ExecutionPolicy Bypass -File ".\setup_forecast_reconciliation_task.ps1"
#
# To remove the task later:
#   Unregister-ScheduledTask -TaskName "OMP Forecast Reconciliation" -Confirm:$false

$TaskName = "OMP Forecast Reconciliation"
$RepoDir  = "H:\2025\NewForecastingModel\OMPforecasting5"
$Script   = "$RepoDir\run_forecast_reconciliation.ps1"
$LogFile  = "$RepoDir\forecast_reconciliation_log.txt"

if (-not (Test-Path $Script)) {
    Write-Error "Script not found: $Script"
    exit 1
}

# The action: run PowerShell non-interactively.
# Logging is handled inside the script itself (Add-Content to forecast_reconciliation_log.txt).
$psArgs = "-NonInteractive -ExecutionPolicy Bypass -File `"$Script`""
$action = New-ScheduledTaskAction `
    -Execute    "powershell.exe" `
    -Argument   $psArgs `
    -WorkingDirectory $RepoDir

# One trigger: Sunday 8:00 PM, weekly -- off-hours, unlikely to overlap any
# ad hoc manual run of the extractor/reconciler/report.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "20:00"

# Settings: 2-hour timeout, skip if previous run still going, catch up on missed runs
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit     (New-TimeSpan -Hours 2) `
    -MultipleInstances      IgnoreNew `
    -StartWhenAvailable     `
    -RunOnlyIfNetworkAvailable

# Principal: current interactive user, no elevation required
$principal = New-ScheduledTaskPrincipal `
    -UserId    "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel  Limited

# Register (overwrites if task already exists under this user)
Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host ""
Write-Host "Task registered: $TaskName"
Write-Host "  Runs as    : $env:USERDOMAIN\$env:USERNAME"
Write-Host "  Trigger    : Sunday 8:00 PM, weekly"
Write-Host "  Script     : $Script"
Write-Host "  Log        : $LogFile"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now  : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Check    : Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Remove   : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host ""
