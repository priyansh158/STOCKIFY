#requires -Version 7.0
# STOCKIFY market daemon launcher (decisions D-27, D-32).
#
# Acts on the daemon's exit code (stockify/daemon/exitcodes.py):
#   0   clean stop: exit
#   75  day done: register a one-shot wake task from next_wake.json, hibernate,
#       restart the daemon after resume
#   76  Kite token expired: restart after $MinDelay. The new process waits for the login itself
#       (it runs the scheduler's catch-up jobs meanwhile), so the launcher never reads the token store
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
$WakeFile = Join-Path $LabDir "next_wake.json"
$WakeTaskName = "STOCKIFY Wake"
$LogDir = Join-Path $env:LOCALAPPDATA "STOCKIFY\logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
# The daemon's own log (stockify-daemon.log) is written, redacted and size-capped, by Python (stockify/log.py). This file
# holds only the launcher's lines and whatever the process wrote to stdout/stderr outside logging (a crash before
# logging starts), passed through Remove-Secrets and trimmed to $LauncherLogMaxBytes at each (re)start.
$LogFile = Join-Path $LogDir "stockify-launcher.log"
$LauncherLogMaxBytes = 5MB

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

# The same blanking as stockify.log.redact: URLs (a ticker's carries the access token), Kite authorization headers and
# token parameters or JSON fields.
function Remove-Secrets([string]$Text) {
    $Text = [regex]::Replace($Text, '\b(?:wss?|https?)://\S+', '<url>', 'IgnoreCase')
    $Text = [regex]::Replace($Text, '\btoken\s+[\w.-]+:[\w.-]+', 'token ***', 'IgnoreCase')
    return [regex]::Replace($Text, '(["'']?)\b(access_token|api_key|api_secret|request_token|refresh_token|enctoken)\b\1(\s*[:=]\s*)(["'']?)[^&\s''",}]*', '$1$2$1$3$4***', 'IgnoreCase')
}

function Write-Log([string]$Message) {
    "[$(Get-Date -Format o)] $Message" | Tee-Object -FilePath $LogFile -Append
}

function Limit-LauncherLog {
    if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt $LauncherLogMaxBytes)) {
        Move-Item -Path $LogFile -Destination "$LogFile.1" -Force
    }
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
    Limit-LauncherLog
    Write-Log "Starting STOCKIFY daemon"
    $started = Get-Date
    & $PyExe @DaemonArgs 2>&1 | ForEach-Object { Remove-Secrets ([string]$_) } | Tee-Object -FilePath $LogFile -Append
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
        Write-Log "Kite token expired (code 76); restarting in $MinDelay s, the daemon waits for the login"
        Start-Sleep -Seconds $MinDelay
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
