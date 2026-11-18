# Creates/repairs the "ClipMachine Autopilot" scheduled task:
#  - runs every 15 minutes, forever
#  - catches up right after the PC wakes from sleep (StartWhenAvailable)
#  - runs on battery too
#  - kills any run that's stuck for over 2 hours, and never starts a second copy on top of a running one
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$here\run-autopilot-hidden.vbs`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "ClipMachine Autopilot" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
$t = Get-ScheduledTask -TaskName "ClipMachine Autopilot"
$i = Get-ScheduledTaskInfo -TaskName "ClipMachine Autopilot"
# Replies now happen right after the machine posts something (no separate 5-minute task).
Unregister-ScheduledTask -TaskName "ClipMachine Replies" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "AUTOPILOT ON. State: $($t.State). Next run: $($i.NextRunTime). Last run: $($i.LastRunTime) (result $($i.LastTaskResult))"
