# Registers the Windows task that boots WSL (and therefore systemd ->
# clickhouse-server + nse-alert.service) when you log on, and keeps the WSL
# VM alive (WSL shuts an idle distro down when no wsl.exe process is attached).
#
# Run in a normal (non-admin) PowerShell:
#   powershell -ExecutionPolicy Bypass -File E:\alert\deploy\register_windows_task.ps1
#
# Same definition as the existing "\NSE Alert System (WSL)" task; re-running
# replaces it. Note: a logon trigger means the system starts after you sign
# in. For start-at-boot without signing in, enable Windows automatic sign-in
# (see AUTOMATION.md) - WSL does not run reliably in a non-interactive
# session-0 task.

$taskName = 'NSE Alert System (WSL)'
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\conhost.exe" `
    -Argument "--headless `"$env:WINDIR\System32\wsl.exe`" -d Ubuntu -u root --exec /bin/sh -c `"systemctl start nse-alert.service; exec sleep infinity`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Keeps WSL Ubuntu running so systemd runs nse-alert.service' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
