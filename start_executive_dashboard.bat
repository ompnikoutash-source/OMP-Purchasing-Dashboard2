@echo off
cd /d "H:\2025\NewForecastingModel\OMPforecasting5"
set SSLKEYLOGFILE=
".venv\Scripts\python.exe" -m streamlit run executive_dashboard.py --server.port 8504
