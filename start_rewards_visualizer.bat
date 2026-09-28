@echo off
SET SSLKEYLOGFILE=
cd /d "H:\2025\NewForecastingModel\OMPforecasting5"
start "" "H:\2025\NewForecastingModel\OMPforecasting5\.venv\Scripts\python.exe" -m streamlit run rewards_program_visualizer.py --server.port 8506
