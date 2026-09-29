@echo off
setlocal
cd /d "%~dp0"

set "PORT=5055"
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="PORT" set "PORT=%%B"
  )
)

set "NGROK_EXE=%~dp0ngrok.exe"
if not exist "%NGROK_EXE%" (
  where ngrok >nul 2>nul
  if errorlevel 1 (
    echo ERROR: ngrok was not found in this folder or PATH.
    echo Copy ngrok.exe into this folder or install ngrok from https://ngrok.com/download.
    pause
    exit /b 1
  )
  set "NGROK_EXE=ngrok"
)

echo Starting ngrok tunnel for local portal port %PORT%...
echo Keep Run Portal.bat open separately.
echo.
"%NGROK_EXE%" http %PORT%
