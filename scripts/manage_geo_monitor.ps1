#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Install", "Start", "Stop", "Status", "Uninstall")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$TaskName = "STOCKIFY Geo Monitor"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_geo_monitor.ps1"
$Pwsh = (Get-Command "pwsh.exe" -ErrorAction Stop).Source

function Get-MonitorTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

switch ($Action) {
    "Install" {
        if (-not (Test-Path (Join-Path $Root ".venv\Scripts\python.exe"))) {
            throw "STOCKIFY .venv is missing. Run .\run.ps1 once before installation."
        }
        $taskAction = New-ScheduledTaskAction `
            -Execute $Pwsh `
            -Argument "-NoProfile -WindowStyle Hidden -File `"$Runner`"" `
            -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # Watchdog, as the recorder's task has (GK 7.7): every 5 minutes the task is started again. With IgnoreNew
        # this does nothing while the monitor runs, and brings it back within 5 minutes if Windows ended it (seen
        # 2026-11-15 18:40: nothing restarted it for about 42 hours). Stop disables the task.
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
            -Description "Always-on STOCKIFY geopolitical news monitor."
        Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
        Write-Host "Installed '$TaskName' for the current Windows user (starts at logon; restarted within 5 minutes if it stops)."
    }
    "Start" {
        if (-not (Get-MonitorTask)) { throw "Task is not installed." }
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started '$TaskName'."
    }
    "Stop" {
        if (-not (Get-MonitorTask)) { throw "Task is not installed." }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null   # or the watchdog trigger would start it again
        Stop-ScheduledTask -TaskName $TaskName
        # Stopping the task ends the launcher, not the Python monitor it started; end this repo's monitor too. The
        # venv's python.exe starts the base interpreter as a child, so stop each venv monitor with its children.
        $procs = @(Get-CimInstance Win32_Process)
        $venvMonitors = $procs | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath -like (Join-Path $Root ".venv\*") -and
            $_.CommandLine -match "-m geo_monitor" }
        foreach ($d in $venvMonitors) {
            $procs | Where-Object { $_.ParentProcessId -eq $d.ProcessId } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $d.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Stopped '$TaskName' (disabled until Start)."
    }
    "Status" {
        $task = Get-MonitorTask
        if (-not $task) {
            Write-Host "Task is not installed."
            exit 1
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Task state: $($task.State)"
        Write-Host "Last run:   $($info.LastRunTime)"
        Write-Host "Last result: $($info.LastTaskResult)"
        $every = ($task.Triggers | Where-Object { $_.Repetition.Interval } | Select-Object -First 1).Repetition.Interval
        Write-Host "Watchdog:   $(if ($every) { "restarts it every $every if it stops" } else { 'none (run Install again to add it)' })"
        $log = Join-Path $env:LOCALAPPDATA "STOCKIFY\logs\geo-monitor.log"
        if (Test-Path $log) {
            Write-Host "Log:        $log"
            Write-Host "Log updated: $((Get-Item $log).LastWriteTime)"
        } else {
            Write-Host "Log:        not created yet"
        }
    }
    "Uninstall" {
        if (Get-MonitorTask) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Write-Host "Uninstalled '$TaskName'."
    }
}
