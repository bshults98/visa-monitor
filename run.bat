@echo off
title BD Visa Dropdown Monitor
cd /d "%~dp0"

echo ============================================================
echo  BD Visa Dropdown Monitor
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found on PATH.
    echo     Install from https://www.python.org/downloads/ and
    echo     tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [*] Installing dependency: requests
    python -m pip install --disable-pip-version-check requests
    if errorlevel 1 (
        echo [!] Failed to install requests.
        pause
        exit /b 1
    )
    echo.
)

echo [*] Starting monitor... (press Ctrl+C to stop)
echo.
python monitor.py %*

echo.
echo Monitor exited.
pause
