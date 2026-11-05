#requires -Version 7.0
# Check the laptop power settings the recorder depends on (D-32). Warns, never
# changes anything. Exit 0 when every setting is as expected, 1 otherwise.
#
#   plugged in: lid close = do nothing, never sleep or hibernate on idle,
#               CPU max <= 80% (heat), wake timers enabled
#   on battery: lid close = do nothing, CPU max <= 80% (keep recording until the battery runs out;
#               Windows hibernates at the critical level)
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Get-PowerValue([string]$Sub, [string]$Setting) {
    $scheme = (& powercfg /getactivescheme) -replace '.*GUID:\s*([0-9a-f-]+).*', '$1'
    $key = "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\$scheme\$Sub\$Setting"
    if (Test-Path $key) {
        $p = Get-ItemProperty $key
        return @{ AC = $p.ACSettingIndex; DC = $p.DCSettingIndex }
    }
    # Not overridden for this scheme: read the effective value from powercfg.
    $out = & powercfg /query SCHEME_CURRENT $Sub $Setting
    $ac = ($out | Select-String "Current AC Power Setting Index: (0x[0-9a-f]+)").Matches.Groups[1].Value
    $dc = ($out | Select-String "Current DC Power Setting Index: (0x[0-9a-f]+)").Matches.Groups[1].Value
    return @{ AC = if ($ac) { [Convert]::ToInt32($ac, 16) } else { $null };
              DC = if ($dc) { [Convert]::ToInt32($dc, 16) } else { $null } }
}

$SubButtons = "4f971e89-eebd-4455-a8de-9e59040e7347"; $LidAction = "5ca83367-6e45-459f-a27b-476b1d01c936"
$SubSleep = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
$StandbyIdle = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"; $HibernateIdle = "9d7815a6-7ee4-497e-8888-515a05f02364"
$RtcWake = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"
$SubProcessor = "54533251-82be-4824-96c1-47b60b740d00"; $ProcMax = "bc5038f7-23e0-4960-96da-33abaf5935ec"

$checks = @(
    @{ Name = "Lid close when plugged in = do nothing"; Value = (Get-PowerValue $SubButtons $LidAction).AC; Ok = { param($v) $v -eq 0 } },
    @{ Name = "Lid close on battery = do nothing";     Value = (Get-PowerValue $SubButtons $LidAction).DC; Ok = { param($v) $v -eq 0 } },
    @{ Name = "Max CPU on battery <= 80%";             Value = (Get-PowerValue $SubProcessor $ProcMax).DC; Ok = { param($v) $v -le 80 } },
    @{ Name = "Idle sleep when plugged in = never";    Value = (Get-PowerValue $SubSleep $StandbyIdle).AC; Ok = { param($v) $v -eq 0 } },
    @{ Name = "Idle hibernate when plugged in = never"; Value = (Get-PowerValue $SubSleep $HibernateIdle).AC; Ok = { param($v) $v -eq 0 } },
    @{ Name = "Wake timers when plugged in = enabled"; Value = (Get-PowerValue $SubSleep $RtcWake).AC; Ok = { param($v) $v -ge 1 } },
    @{ Name = "Max CPU when plugged in <= 80%";        Value = (Get-PowerValue $SubProcessor $ProcMax).AC; Ok = { param($v) $v -le 80 } }
)

$bad = 0
foreach ($c in $checks) {
    $ok = $null -ne $c.Value -and (& $c.Ok $c.Value)
    if ($ok) {
        Write-Host ("  ok    {0} ({1})" -f $c.Name, $c.Value)
    } else {
        Write-Warning ("power setting drifted: {0} (now {1}); see docs/plan/00_overview.md D-32" -f $c.Name, $c.Value)
        $bad++
    }
}
$hibernate = ((& powercfg /a) -join "`n") -match "(?m)^\s+Hibernate\s*$"
if (-not $hibernate) {
    Write-Warning "hibernate is not available; the daemon will stay awake instead of sleeping after the close"
    $bad++
}
exit ([int]($bad -gt 0))
