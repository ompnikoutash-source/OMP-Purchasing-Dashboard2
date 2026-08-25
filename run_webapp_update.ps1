$ErrorActionPreference = "Stop"

$RepoDir   = "H:\2025\NewForecastingModel\OMPforecasting5"
$PythonExe = "$RepoDir\.venv\Scripts\python.exe"
$LogFile   = "$RepoDir\refresh_log.txt"
$StatusFile = "$RepoDir\_refresh_status.json"

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
Write-Log "INFO" "OMP Dashboard refresh started"

$results = @{}

$results["Flooring"] = Run-PythonStep -StepName "Flooring" -ScriptArgs @(".\flooringwebapp.py")
$results["Sundries"] = Run-PythonStep -StepName "Sundries" -ScriptArgs @(".\sundrieswebapp_refactored.py")
$results["Moulding"] = Run-PythonStep -StepName "Moulding" -ScriptArgs @(".\mouldingwebapp_refactored.py")

# Write status file so the dashboard displays an accurate last-run timestamp
$allOk = $results.Values -notcontains $false
$statusObj = [ordered]@{
    last_run = (Get-Date -Format "o")
    results  = @{
        Flooring = $results["Flooring"]
        Sundries = $results["Sundries"]
        Moulding = $results["Moulding"]
    }
    all_ok = $allOk
}
$statusObj | ConvertTo-Json | Set-Content -Path $StatusFile -Encoding utf8
Write-Log "INFO" "Status file updated: all_ok=$allOk"

# Stage JSON outputs and commit if anything changed
$filesToStage = @(".\flooringwebappJSON", ".\sundrieswebappJSON", ".\mouldingwebappJSON", ".\$([System.IO.Path]::GetFileName($StatusFile))")
git add @filesToStage
$changes = git status --porcelain
if ($changes) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    git commit -m "Auto update JSON $ts"
    git push
    Write-Log "INFO" "Git commit and push complete"
} else {
    Write-Log "INFO" "No JSON changes to commit"
}

$ok  = ($results.Values | Where-Object { $_ -eq $true }).Count
$tot = $results.Count
Write-Log "INFO" "Refresh complete: $ok/$tot scripts succeeded"
Write-Log "INFO" "============================================================"
