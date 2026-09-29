@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo Odoo to Ginesys - Environment Setup
echo ==========================================
echo.

set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo ERROR: Python 3 was not found.
  echo Install Python 3.11+ from python.org and select "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :fail
)

echo Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :fail

if not exist ".env" copy /Y ".env.example" ".env" >nul

echo.
echo Setup completed successfully.
echo Next: start the portal and authorize each Ginesys username and role in Admin.
pause
exit /b 0

:fail
echo.
echo ERROR: Setup failed.
echo The script always uses the project root, so no backend\backend path is required.
pause
exit /b 1
