@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Environment not found. Running setup first...
  call "%~dp0Setup or Repair Environment.bat"
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" "%~dp0run.py"
if errorlevel 1 (
  echo.
  echo Portal stopped with an error.
  pause
)
