#requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$DryRun,
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PyExe = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $PyExe)) {
    throw "STOCKIFY .venv is missing. Run .\run.ps1 once before starting the monitor."
}
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is not set."
}

$LogDir = Join-Path $env:LOCALAPPDATA "STOCKIFY\logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogFile = Join-Path $LogDir "geo-monitor.log"

$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$Root;$env:PYTHONPATH"
} else {
    $Root
}
$env:PYTHONUNBUFFERED = "1"

$MonitorArgs = @("-m", "geo_monitor")
if ($Once) { $MonitorArgs += "--once" }
if ($DryRun) { $MonitorArgs += "--dry-run" }
if ($VerboseOutput) { $MonitorArgs += "--verbose" }

Set-Location $Root
"[$(Get-Date -Format o)] Starting geo monitor" |
    Tee-Object -FilePath $LogFile -Append
& $PyExe @MonitorArgs 2>&1 | Tee-Object -FilePath $LogFile -Append
exit $LASTEXITCODE
