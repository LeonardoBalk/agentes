@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B main.py --open
) else (
  python -B main.py --open
)
if errorlevel 1 pause
