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
            -Argument "-NoProfile -File `"$Runner`"" `
            -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
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
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Description "Always-on STOCKIFY geopolitical news monitor."
        Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
        Write-Host "Installed '$TaskName' for the current Windows user."
    }
    "Start" {
        if (-not (Get-MonitorTask)) { throw "Task is not installed." }
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started '$TaskName'."
    }
    "Stop" {
        if (-not (Get-MonitorTask)) { throw "Task is not installed." }
        Stop-ScheduledTask -TaskName $TaskName
        Write-Host "Stopped '$TaskName'."
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
