@echo off
setlocal

rem Always run from the project directory, even when this file is double-clicked.
cd /d "%~dp0"

set "PROJECT_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\data\playwright-browsers"
set "NODE_OPTIONS=--use-system-ca"
set "PYTHONUTF8=1"

if not exist "%PROJECT_PYTHON%" (
    echo.
    echo The project Python environment was not found:
    echo   %PROJECT_PYTHON%
    echo.
    echo Create it and install the requirements first:
    echo   py -3.11 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

set "PROJECT_CHROMIUM_FOUND="
for /d %%D in ("%PLAYWRIGHT_BROWSERS_PATH%\chromium-*") do (
    if exist "%%~fD\chrome-win64\chrome.exe" set "PROJECT_CHROMIUM_FOUND=1"
)

if not defined PROJECT_CHROMIUM_FOUND (
    echo.
    echo Installing the Chromium browser required by the web page inspector...
    "%PROJECT_PYTHON%" -m playwright install chromium
    if errorlevel 1 (
        echo.
        echo Chromium installation failed. Review the message above, then try again.
        pause
        exit /b 1
    )
)

if /i "%~1"=="--check" (
    echo Launcher check passed: project Python and Chromium are available.
    exit /b 0
)

netstat -ano | findstr /R /C:"127.0.0.1:5000 .*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo Port 5000 is already in use, so another copy of the app may still be running.
    echo Close the older Flask or Python process, then run this launcher again.
    echo This check prevents an older process from silently serving stale code.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting the Social OSINT Query Console with the project Python environment.
echo Open http://127.0.0.1:5000 in your browser.
echo Keep this window open while using the console. Press Ctrl+C to stop it.
echo.

"%PROJECT_PYTHON%" -m flask --app app run --host 127.0.0.1 --port 5000

echo.
echo The Social OSINT Query Console has stopped.
pause
