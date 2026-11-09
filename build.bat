@echo off
REM Builds dist\Truck Dispatcher\ (the exe and its files) and dist\TruckDispatcher-vX.Y.Z.zip
cd /d "%~dp0"
python tools\build.py
pause
