#requires -Version 7.0
# STOCKIFY market daemon launcher (decisions D-27, D-32).
#
# Acts on the daemon's exit code (stockify/daemon/exitcodes.py):
#   0   clean stop: exit
#   75  day done: register a one-shot wake task from next_wake.json, hibernate,
#       restart the daemon after resume
#   76  Kite token stale or expired: wait for a fresh kite_token.json, restart
#   79  another daemon already holds the lab's lock: exit (never restart into it)
#   80  the engine thread was stuck at shutdown: restart (the journals resume by replay)
#   81  the lab's lock file cannot be opened: exit (a restart would fail the same way)
#   other: crash or gave up reconnecting: restart with backoff
#          (5 s doubling to 5 min; reset after 10 min of uptime)
[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$VerboseOutput,
    [switch]$NoHibernate,      # exit 75 waits for the wake time instead of hibernating (testing)
    [switch]$SleepTest         # owner check: wake in 5 minutes, hibernate now, report the resume
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

$StateDir = Join-Path $env:LOCALAPPDATA "STOCKIFY\kite"
$LabDir = Join-Path $StateDir "lab"
$TokenFile = Join-Path $StateDir "kite_token.json"
$WakeFile = Join-Path $LabDir "next_wake.json"
$WakeTaskName = "STOCKIFY Wake"
$LogDir = Join-Path $env:LOCALAPPDATA "STOCKIFY\logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogFile = Join-Path $LogDir "stockify-daemon.log"

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root;$env:PYTHONPATH" } else { $Root }
$env:PYTHONUNBUFFERED = "1"

$DaemonArgs = @("-m", "stockify")
if ($VerboseOutput) { $DaemonArgs += "--verbose" }
$DaemonArgs += "daemon"
if ($Once) { $DaemonArgs += "--once" }

$ExitSleep = 75
$ExitToken = 76
$ExitAlreadyRunning = 79
$ExitEngineStuck = 80
$ExitLockFailed = 81

function Write-Log([string]$Message) {
    "[$(Get-Date -Format o)] $Message" | Tee-Object -FilePath $LogFile -Append
}

function Test-HibernateAvailable {
    $states = (& powercfg /a) -join "`n"
    return ($states -match "(?m)^\s+Hibernate\s*$")
}

function Register-WakeTask([datetime]$WakeLocal) {
    # A one-shot task whose only job is to wake the laptop (D-32).
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c exit 0"
    $trigger = New-ScheduledTaskTrigger -Once -At $WakeLocal
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -DeleteExpiredTaskAfter (New-TimeSpan -Days 1)
    $trigger.EndBoundary = $WakeLocal.AddHours(2).ToString("s")
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "Wakes the laptop for the next NSE session (STOCKIFY)."
    Register-ScheduledTask -TaskName $WakeTaskName -InputObject $task -Force | Out-Null
}

function Invoke-SleepUntilWake {
    if (-not (Test-Path $WakeFile)) {
        Write-Log "exit 75 without $WakeFile; restarting the daemon instead of sleeping"
        return
    }
    $info = Get-Content $WakeFile -Raw | ConvertFrom-Json
    $wakeLocal = [DateTimeOffset]::FromUnixTimeSeconds([int64]$info.wake_unix_s).LocalDateTime
    Register-WakeTask $wakeLocal
    Write-Log "wake task set for $($wakeLocal.ToString('yyyy-MM-dd HH:mm')) local ($($info.wake_ist) IST)"
    if ($NoHibernate -or -not (Test-HibernateAvailable)) {
        if (-not $NoHibernate) { Write-Log "hibernate is not available; waiting awake instead" }
        while ((Get-Date) -lt $wakeLocal) { Start-Sleep -Seconds 30 }
        return
    }
    Write-Log "hibernating"
    & shutdown.exe /h
    # shutdown /h returns before the machine goes down; give it time, then
    # continue after resume.
    Start-Sleep -Seconds 60
    Write-Log "resumed from hibernate"
}

function Wait-FreshToken([datetime]$Since) {
    Write-Log "waiting for a fresh Kite login (log in at http://127.0.0.1:3000/portfolio)"
    while ($true) {
        if ((Test-Path $TokenFile) -and ((Get-Item $TokenFile).LastWriteTime -gt $Since)) {
            Write-Log "fresh Kite token found"
            return
        }
        Start-Sleep -Seconds 30
    }
}

if ($SleepTest) {
    $wake = [DateTimeOffset]::UtcNow.AddMinutes(5)
    New-Item -ItemType Directory -Path $LabDir -Force | Out-Null
    @{ wake_unix_s = $wake.ToUnixTimeSeconds(); wake_ist = "sleep test" } | ConvertTo-Json | Set-Content $WakeFile
    Write-Log "sleep test: the laptop should hibernate now and wake by itself at $($wake.LocalDateTime.ToString('HH:mm'))"
    $before = Get-Date
    Invoke-SleepUntilWake
    $slept = (Get-Date) - $before
    Write-Log ("sleep test finished: back after {0:N1} min" -f $slept.TotalMinutes)
    Remove-Item $WakeFile -ErrorAction SilentlyContinue
    exit 0
}

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
    if ($code -eq $ExitAlreadyRunning) {
        Write-Log "another STOCKIFY daemon is already running on this lab (code 79); not restarting"
        exit $code
    }
    if ($code -eq $ExitLockFailed) {
        Write-Log "the STOCKIFY daemon cannot take the lab's lock (code 81); not restarting"
        exit $code
    }
    if ($code -eq $ExitEngineStuck) {
        Write-Log "the engine thread was stuck at shutdown (code 80); restarting, the journals resume by replay"
    }
    if ($code -eq $ExitSleep) {
        Write-Log "day done (code 75)"
        Invoke-SleepUntilWake
        $delay = $MinDelay
        continue
    }
    if ($code -eq $ExitToken) {
        Wait-FreshToken $started
        $delay = $MinDelay
        continue
    }
    if (((Get-Date) - $started) -ge $StableAfter) {
        $delay = $MinDelay
    }
    Write-Log "STOCKIFY daemon stopped with code $code; restarting in $delay s"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, $MaxDelay)
}
