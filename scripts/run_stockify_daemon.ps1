#requires -Version 7.0
# STOCKIFY market daemon launcher with restart-on-crash backoff (decision D-27).
[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PyExe = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $PyExe)) {
    throw "STOCKIFY .venv is missing. Run .\run.ps1 once before starting the daemon."
}
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is not set."
}

$LogDir = Join-Path $env:LOCALAPPDATA "STOCKIFY\logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogFile = Join-Path $LogDir "stockify-daemon.log"

$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$Root;$env:PYTHONPATH"
} else {
    $Root
}
$env:PYTHONUNBUFFERED = "1"

$DaemonArgs = @("-m", "stockify")
if ($VerboseOutput) { $DaemonArgs += "--verbose" }
$DaemonArgs += "daemon"
if ($Once) { $DaemonArgs += "--once" }

function Write-Log([string]$Message) {
    "[$(Get-Date -Format o)] $Message" | Tee-Object -FilePath $LogFile -Append
}

# Restart after a crash: 5 s, doubling to a 5 min ceiling. A run that stayed
# up for 10 minutes resets the delay. Exit code 0 means a clean stop.
$MinDelay = 5
$MaxDelay = 300
$StableAfter = [TimeSpan]::FromMinutes(10)
$delay = $MinDelay

Set-Location $Root
while ($true) {
    Write-Log "Starting STOCKIFY daemon"
    $started = Get-Date
    & $PyExe @DaemonArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $Once) {
        Write-Log "STOCKIFY daemon exited with code $code"
        exit $code
    }
    if (((Get-Date) - $started) -ge $StableAfter) {
        $delay = $MinDelay
    }
    Write-Log "STOCKIFY daemon crashed with code $code; restarting in $delay s"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, $MaxDelay)
}
