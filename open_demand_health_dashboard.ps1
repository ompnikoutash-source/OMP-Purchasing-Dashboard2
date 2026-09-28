# Opens the latest generated Demand Health dashboard in your default browser.
$Dashboard = Join-Path $PSScriptRoot "demand_health_dashboard_live.html"

if (-not (Test-Path -LiteralPath $Dashboard)) {
    throw "Could not find demand_health_dashboard_live.html next to this script."
}

Start-Process -FilePath $Dashboard
