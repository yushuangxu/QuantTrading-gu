@echo off
setlocal

cd /d "%~dp0.."

set "PID_FILE=runtime\quant_alert.pid"
set "LOG_META_FILE=runtime\current_logs.env"
set "LOG_FILE=logs\quant_alert.log"
set "ERR_LOG_FILE=logs\quant_alert.err.log"

if exist "%LOG_META_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%LOG_META_FILE%") do (
        if /I "%%a"=="LOG_FILE" set "LOG_FILE=%%b"
        if /I "%%a"=="ERR_LOG_FILE" set "ERR_LOG_FILE=%%b"
    )
)

if not exist "%PID_FILE%" (
    echo quant_alert is not running. ^(PID file not found^)
    if exist "%LOG_FILE%" (
        echo.
        echo ===== Last 20 log lines =====
        powershell -NoProfile -Command "Get-Content -Path '%LOG_FILE%' -Tail 20"
    )
    exit /b 0
)

set /p PID=<"%PID_FILE%"
if "%PID%"=="" (
    echo quant_alert status unknown. PID file is empty.
    exit /b 1
)

tasklist /FI "PID eq %PID%" | findstr /R /C:" %PID% " >nul
if errorlevel 1 (
    echo quant_alert is not running. stale PID=%PID%
) else (
    echo quant_alert is running. PID=%PID%
)

if exist "%LOG_FILE%" (
    echo.
    echo ===== Last 20 log lines =====
    powershell -NoProfile -Command "Get-Content -Path '%LOG_FILE%' -Tail 20"
) else (
    echo log file not found: %LOG_FILE%
)

if exist "%ERR_LOG_FILE%" (
    echo.
    echo ===== Last 20 error log lines =====
    powershell -NoProfile -Command "Get-Content -Path '%ERR_LOG_FILE%' -Tail 20"
)

exit /b 0
