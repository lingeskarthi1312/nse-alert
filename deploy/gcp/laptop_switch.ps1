# Stop or restart the LAPTOP copy of the alert system.
# Only ONE copy may run, otherwise every email is sent twice (and the ngrok link
# can only be served by one machine).
#
#   powershell -ExecutionPolicy Bypass -File E:\alert\deploy\gcp\laptop_switch.ps1 off   # after the cloud server works
#   powershell -ExecutionPolicy Bypass -File E:\alert\deploy\gcp\laptop_switch.ps1 on    # fall back to the laptop
param([Parameter(Mandatory = $true)][ValidateSet('on', 'off')][string]$Mode)

$task = 'NSE Alert System (WSL)'
if ($Mode -eq 'off') {
    wsl.exe -d Ubuntu -u root -- systemctl disable --now nse-alert.service nse-dashboard.service nse-tunnel.service
    Disable-ScheduledTask -TaskName $task | Out-Null
    Write-Host 'Laptop copy stopped and will not start at logon. The cloud server is now the only sender.'
} else {
    wsl.exe -d Ubuntu -u root -- systemctl enable --now nse-alert.service nse-dashboard.service nse-tunnel.service
    Enable-ScheduledTask -TaskName $task | Out-Null
    Start-ScheduledTask -TaskName $task
    Write-Host 'Laptop copy running again. Stop the cloud server (sudo systemctl disable --now nse-alert nse-tunnel) to avoid double emails.'
}
wsl.exe -d Ubuntu -u root -- systemctl is-active nse-alert.service
Get-ScheduledTask -TaskName $task | Select-Object TaskName, State
