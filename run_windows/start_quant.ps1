param(
    [Parameter(Mandatory = $true)]
    [string]$WorkDir
)

$ErrorActionPreference = "Stop"

$today = Get-Date -Format "yyyy-MM-dd"
$logPath = Join-Path $WorkDir ("logs\" + $today + "_quant_alert.log")
$errLogPath = Join-Path $WorkDir ("logs\" + $today + "_quant_alert.err.log")
$pidPath = Join-Path $WorkDir "runtime\quant_alert.pid"
$logMetaPath = Join-Path $WorkDir "runtime\current_logs.env"

if ($env:CONDA_EXE -and (Test-Path $env:CONDA_EXE)) {
    $condaExe = $env:CONDA_EXE
} else {
    $cmd = Get-Command conda -ErrorAction Stop
    $condaExe = $cmd.Source
}

if (-not (Test-Path $logPath)) { New-Item -Path $logPath -ItemType File | Out-Null }
if (-not (Test-Path $errLogPath)) { New-Item -Path $errLogPath -ItemType File | Out-Null }

$proc = Start-Process `
    -FilePath $condaExe `
    -ArgumentList @("run", "--no-capture-output", "-n", "quant", "python", "main.py") `
    -WorkingDirectory $WorkDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logPath `
    -RedirectStandardError $errLogPath `
    -PassThru
Set-Content -Path $pidPath -Value $proc.Id -Encoding ASCII
Set-Content -Path $logMetaPath -Encoding ASCII -Value @(
    "LOG_FILE=$logPath"
    "ERR_LOG_FILE=$errLogPath"
)
