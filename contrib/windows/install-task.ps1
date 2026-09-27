# Registers a logon task that runs `aidt watch` in the background.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File install-task.ps1
$aidt = (Get-Command aidt).Source
$action = New-ScheduledTaskAction -Execute $aidt -Argument "watch"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ai-drone-tune watch" -Action $action -Trigger $trigger -Settings $settings -Description "Betaflight blackbox download / auto-tune on USB connect"
Write-Host "Installed. For interactive approval run 'aidt watch' in a terminal instead."
