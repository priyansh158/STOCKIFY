#requires -Version 7.0
# Install the STOCKIFY market daemon as a per-user Scheduled Task that starts at
# logon (D-32). The task runs scripts/run_stockify_daemon.ps1, which records each
# session, hibernates after the close and wakes the laptop for the next one.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Install", "Start", "Stop", "Status", "Uninstall")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$TaskName = "STOCKIFY Market Daemon"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_stockify_daemon.ps1"
$Pwsh = (Get-Command "pwsh.exe" -ErrorAction Stop).Source

function Get-DaemonTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

switch ($Action) {
    "Install" {
        if (-not (Test-Path (Join-Path $Root ".venv\Scripts\python.exe"))) {
            throw "STOCKIFY .venv is missing. Run .\run.ps1 once before installation."
        }
        & (Join-Path $PSScriptRoot "check_power.ps1")
        $taskAction = New-ScheduledTaskAction `
            -Execute $Pwsh `
            -Argument "-NoProfile -WindowStyle Hidden -File `"$Runner`"" `
            -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # Watchdog: every 5 minutes the task is started again. With IgnoreNew this does nothing while the launcher
        # runs, and brings it back within 5 minutes if Windows ended it (the launcher restarts the daemon itself,
        # but nothing restarted the launcher when it was killed; seen twice on 2026-11-15). Stop disables the task.
        $watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $settings = New-ScheduledTaskSettingsSet `
            -MultipleInstances IgnoreNew `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        $principal = New-ScheduledTaskPrincipal `
            -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType Interactive `
            -RunLevel Limited
        $task = New-ScheduledTask `
            -Action $taskAction `
            -Trigger @($trigger, $watchdog) `
            -Settings $settings `
            -Principal $principal `
            -Description "STOCKIFY market daemon: records NSE sessions, hibernates after the close."
        Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
        Write-Host "Installed '$TaskName' for the current Windows user (starts at logon; restarted within 5 minutes if it stops)."
    }
    "Start" {
        if (-not (Get-DaemonTask)) { throw "Task is not installed." }
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started '$TaskName'."
    }
    "Stop" {
        if (-not (Get-DaemonTask)) { throw "Task is not installed." }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null   # or the watchdog trigger would start it again
        Stop-ScheduledTask -TaskName $TaskName
        # Stopping the task ends the launcher but not the Python daemon it started (seen 2026-11-16: the daemon kept
        # running on old code and held the log open). End this repo's daemon processes too; journals recover on start.
        # The venv's python.exe starts the base interpreter as a child, so stop each venv daemon with its children.
        $procs = @(Get-CimInstance Win32_Process)
        $venvDaemons = $procs | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath -like (Join-Path $Root ".venv\*") -and
            $_.CommandLine -match "-m stockify daemon" }
        foreach ($d in $venvDaemons) {
            $procs | Where-Object { $_.ParentProcessId -eq $d.ProcessId } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $d.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Stopped '$TaskName' (disabled until Start)."
    }
    "Status" {
        $task = Get-DaemonTask
        if (-not $task) {
            Write-Host "Task is not installed."
            exit 1
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Task state:  $($task.State)"
        Write-Host "Last run:    $($info.LastRunTime)"
        Write-Host "Last result: $($info.LastTaskResult)"
        $every = ($task.Triggers | Where-Object { $_.Repetition.Interval } | Select-Object -First 1).Repetition.Interval
        Write-Host "Watchdog:    $(if ($every) { "restarts it every $every if it stops" } else { 'none (run Install again to add it)' })"
        $wake = Get-ScheduledTask -TaskName "STOCKIFY Wake" -ErrorAction SilentlyContinue
        if ($wake) {
            Write-Host "Next wake:   $(($wake.Triggers | Select-Object -First 1).StartBoundary)"
        }
        $log = Join-Path $env:LOCALAPPDATA "STOCKIFY\logs\stockify-daemon.log"
        if (Test-Path $log) {
            Write-Host "Log:         $log (updated $((Get-Item $log).LastWriteTime))"
        } else {
            Write-Host "Log:         not created yet"
        }
    }
    "Uninstall" {
        if (Get-DaemonTask) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        if (Get-ScheduledTask -TaskName "STOCKIFY Wake" -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName "STOCKIFY Wake" -Confirm:$false
        }
        Write-Host "Uninstalled '$TaskName'."
    }
}
