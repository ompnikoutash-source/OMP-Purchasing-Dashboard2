param(
    [switch]$DryRun,
    [switch]$NoBackup,
    [string]$JsonPath = ""
)

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $RepoDir ".venv\Scripts\python.exe"
$RefreshScript = Join-Path $RepoDir "refresh_gartman_dashboard_data.py"
$LogFile = Join-Path $RepoDir "refresh_gartman_dashboard_data.log"

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

if (-not (Test-Path $RefreshScript)) {
    throw "Refresh script not found: $RefreshScript"
}

Set-Location $RepoDir

$argsList = @($RefreshScript)
if ($DryRun) {
    $argsList += "--dry-run"
}
if ($NoBackup) {
    $argsList += "--no-backup"
}
if ($JsonPath.Trim()) {
    $argsList += "--json-path"
    $argsList += $JsonPath
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$timestamp [INFO] Starting Gartman dashboard data refresh" | Tee-Object -FilePath $LogFile -Append

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PythonExe @argsList 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $LogFile -Append
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
if ($exitCode -eq 0) {
    "$timestamp [INFO] Gartman dashboard data refresh completed successfully" | Tee-Object -FilePath $LogFile -Append
    Write-Host ""
    Write-Host "Done. Refresh the browser page after Streamlit's short cache expires, or restart the Streamlit app for immediate pickup."
} else {
    "$timestamp [ERROR] Gartman dashboard data refresh failed with exit code $exitCode" | Tee-Object -FilePath $LogFile -Append
    exit $exitCode
}
