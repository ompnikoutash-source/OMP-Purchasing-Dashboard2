@echo off
REM PO Arrival Timeline — Task Scheduler launcher
REM Calls the venv Python directly to avoid PowerShell execution policy issues.
REM Recommended interval: every 5 minutes, 7:00 AM to 7:00 PM.

cd /d "H:\2025\NewForecastingModel\OMPforecasting5"
"H:\2025\NewForecastingModel\OMPforecasting5\.venv\Scripts\python.exe" refresh_po_arrivals.py
