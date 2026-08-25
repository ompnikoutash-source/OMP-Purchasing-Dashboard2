# setup_omp_refresh_task.ps1
#
# Creates (or replaces) a Windows scheduled task that runs the OMP dashboard
# refresh twice daily (7:30 AM and 1:30 PM) as the current logged-in user.
#
# Does NOT require Administrator rights — the task is owned by the current
# user account and is fully visible in Task Scheduler without elevation.
#
# Usage (run once from any PowerShell prompt — no "Run as Administrator"):
#   PowerShell -ExecutionPolicy Bypass -File ".\setup_omp_refresh_task.ps1"
#
# To remove the task later:
#   Unregister-ScheduledTask -TaskName "OMP Dashboard Refresh" -Confirm:$false

$TaskName   = "OMP Dashboard Refresh"
$RepoDir    = "H:\2025\NewForecastingModel\OMPforecasting5"
$Script     = "$RepoDir\run_webapp_update.ps1"
$LogFile    = "$RepoDir\refresh_log.txt"

if (-not (Test-Path $Script)) {
    Write-Error "Script not found: $Script"
    exit 1
}

# The action: run PowerShell non-interactively.
# Logging is handled inside the script itself (Add-Content to refresh_log.txt).
$psArgs = "-NonInteractive -ExecutionPolicy Bypass -File `"$Script`""
$action = New-ScheduledTaskAction `
    -Execute    "powershell.exe" `
    -Argument   $psArgs `
    -WorkingDirectory $RepoDir

# Two triggers: 11:00 AM and 2:00 PM every day
$trigger1 = New-ScheduledTaskTrigger -Daily -At "11:00"
$trigger2 = New-ScheduledTaskTrigger -Daily -At "14:00"

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
    -Trigger   @($trigger1, $trigger2) `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host ""
Write-Host "Task registered: $TaskName"
Write-Host "  Runs as    : $env:USERDOMAIN\$env:USERNAME"
Write-Host "  Triggers   : 11:00 AM and 2:00 PM daily"
Write-Host "  Script     : $Script"
Write-Host "  Log        : $LogFile"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Run now  : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Check    : Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Remove   : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host ""
