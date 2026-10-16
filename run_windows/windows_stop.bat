@echo off
setlocal

cd /d "%~dp0.."

set "PID_FILE=runtime\quant_alert.pid"

if not exist "%PID_FILE%" (
    echo PID file not found. quant_alert may not be running.
    exit /b 0
)

set /p PID=<"%PID_FILE%"
if "%PID%"=="" (
    echo PID file is empty, cleaning it.
    del /f /q "%PID_FILE%" >nul 2>&1
    exit /b 0
)

tasklist /FI "PID eq %PID%" | findstr /R /C:" %PID% " >nul
if errorlevel 1 (
    echo Process %PID% not found, cleaning stale PID file.
    del /f /q "%PID_FILE%" >nul 2>&1
    exit /b 0
)

taskkill /PID %PID% /T /F >nul
if errorlevel 1 (
    echo Failed to stop PID %PID%.
    exit /b 1
)

del /f /q "%PID_FILE%" >nul 2>&1
echo quant_alert stopped. PID=%PID%
exit /b 0
