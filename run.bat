@echo off
REM Runs the Truck Dispatcher from source (Python 3.11+).
cd /d "%~dp0"
python -c "import numpy, scipy, msgpack, zstandard" 2>nul || python -m pip install -r requirements.txt
python app.py
if errorlevel 1 pause
