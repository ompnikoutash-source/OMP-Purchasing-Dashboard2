$ErrorActionPreference = "Stop"

$RepoDir   = "H:\2025\NewForecastingModel\OMPforecasting5"
$PythonExe = "$RepoDir\.venv\Scripts\python.exe"
$LogFile   = "$RepoDir\forecast_reconciliation_log.txt"

Set-Location $RepoDir

function Write-Log {
    param([string]$Level, [string]$Msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts [$Level] $Msg"
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

function Run-PythonStep {
    param(
        [Parameter(Mandatory = $true)][string]$StepName,
        [Parameter(Mandatory = $true)][string[]]$ScriptArgs
    )
    Write-Log "INFO" "$StepName`: starting"
    $start = Get-Date
    & $PythonExe @ScriptArgs
    $elapsed = [int]((Get-Date) - $start).TotalSeconds
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR" "$StepName`: failed (exit $LASTEXITCODE) after ${elapsed}s"
        return $false
    }
    Write-Log "INFO" "$StepName`: completed in ${elapsed}s"
    return $true
}

Write-Log "INFO" "============================================================"
Write-Log "INFO" "Forecast accuracy reconciliation started"

# Three steps, in order, against agents/forecast_history.db. Deliberately
# sequential and NOT run alongside run_webapp_update.ps1 or any other script
# touching that database -- this network share (\\server\...) does not
# reliably honor SQLite's busy_timeout/locking, so any overlap risks a
# "database is locked" failure or a genuine hang. This script never touches
# flooringwebappJSON/sundrieswebappJSON/mouldingwebappJSON or git, so it has
# no interaction with run_webapp_update.ps1's own commit/push step.
$results = @{}
$results["Extractor"]  = Run-PythonStep -StepName "Extractor"  -ScriptArgs @("-m", "agents.forecast_history_extractor")
$results["Reconciler"] = Run-PythonStep -StepName "Reconciler" -ScriptArgs @("-m", "agents.forecast_reconciler")
$results["Report"]     = Run-PythonStep -StepName "Report"     -ScriptArgs @("-m", "agents.forecast_accuracy_report")

$ok  = ($results.Values | Where-Object { $_ -eq $true }).Count
$tot = $results.Count
Write-Log "INFO" "Reconciliation complete: $ok/$tot steps succeeded"
Write-Log "INFO" "============================================================"
