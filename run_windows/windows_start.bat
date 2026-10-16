@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0.."

set "PID_DIR=runtime"
set "LOG_DIR=logs"
set "PID_FILE=%PID_DIR%\quant_alert.pid"
set "LOG_META_FILE=%PID_DIR%\current_logs.env"

if not exist "%PID_DIR%" mkdir "%PID_DIR%"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if exist "%PID_FILE%" (
    set /p EXIST_PID=<"%PID_FILE%"
    if not "%EXIST_PID%"=="" (
        tasklist /FI "PID eq %EXIST_PID%" | findstr /R /C:" %EXIST_PID% " >nul
        if not errorlevel 1 (
            echo quant_alert is already running with PID %EXIST_PID%.
            exit /b 0
        )
    )
    del /f /q "%PID_FILE%" >nul 2>&1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_quant.ps1" -WorkDir "%cd%"

if errorlevel 1 (
    echo Failed to start quant_alert.
    exit /b 1
)

set /p NEW_PID=<"%PID_FILE%"
set "LOG_FILE="
set "ERR_LOG_FILE="
if exist "%LOG_META_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%LOG_META_FILE%") do (
        if /I "%%a"=="LOG_FILE" set "LOG_FILE=%%b"
        if /I "%%a"=="ERR_LOG_FILE" set "ERR_LOG_FILE=%%b"
    )
)
echo quant_alert started. PID=%NEW_PID%
if not "%LOG_FILE%"=="" (
    echo log file: %LOG_FILE%
)
if not "%ERR_LOG_FILE%"=="" (
    echo err log file: %ERR_LOG_FILE%
)
exit /b 0
