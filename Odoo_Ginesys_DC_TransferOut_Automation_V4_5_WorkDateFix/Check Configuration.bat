@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Environment not found. Please run Setup or Repair Environment.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "%~dp0check_config.py"
echo.
pause
