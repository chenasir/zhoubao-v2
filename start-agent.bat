@echo off
setlocal
title CICC Weekly Agent
cd /d "%~dp0"

echo.
echo ================================================================
echo   CICC Weekly Agent - starting
echo ================================================================
echo   If browser does not open automatically, visit:
echo   http://127.0.0.1:8765
echo ================================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul
    if errorlevel 1 python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Python was not found. Please install Python first.
        pause
        exit /b 1
    )

    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency install failed.
        pause
        exit /b 1
    )
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [INFO] .env created from .env.example
        echo Please open .env and fill OPENROUTER_API_KEY, then run again.
        pause
        exit /b 0
    )
)

echo Starting server...
".venv\Scripts\python.exe" run.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo Server stopped with exit code %EXITCODE%.
echo If the page did not open automatically, open:
echo http://127.0.0.1:8765
pause
exit /b %EXITCODE%
