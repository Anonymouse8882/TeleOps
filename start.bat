@echo off
rem Start TeleOps in the foreground (console window shows the log).
rem Closing the window stops the server. Use start-background.vbs to keep it
rem running detached. Keep this file pure ASCII - see stop.bat for why.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run these first:
  echo     python -m venv .venv
  echo     .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)

".venv\Scripts\python.exe" run.py %*
pause
