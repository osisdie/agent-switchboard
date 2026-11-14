<#
.SYNOPSIS
  Register a Windows scheduled task that starts the Claude Code fleet inside WSL at logon.

.DESCRIPTION
  The task runs:  wsl.exe -d <Distro> -- bash -lc "$HOME/.local/bin/fleet-up"
  ($HOME is expanded by bash inside WSL, not by PowerShell.)
  By default the console window is hidden via `conhost.exe --headless` (Windows 11 / recent
  Windows 10). Re-running the script replaces the existing task (idempotent).

.PARAMETER Distro
  WSL distribution name (see `wsl.exe -l -q`). Default: Ubuntu.

.PARAMETER TaskName
  Scheduled task name. Default: claude-fleet.

.PARAMETER Visible
  Do not hide the console window (useful for debugging).

.PARAMETER KeepAlive
  Keep the wsl.exe process attached after fleet-up (`exec sleep infinity`), for setups where
  WSL shuts the distro down once no wsl.exe client is attached.

.PARAMETER Unregister
  Remove the task and exit.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\windows-task.ps1 -Distro Ubuntu
#>
[CmdletBinding()]
param(
  [string]$Distro = 'Ubuntu',
  [string]$TaskName = 'claude-fleet',
  [switch]$Visible,
  [switch]$KeepAlive,
  [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Unregister) {
  if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
  } else {
    Write-Host "No scheduled task named '$TaskName'."
  }
  return
}

# Single quotes: $HOME must reach bash unexpanded.
$inner = '$HOME/.local/bin/fleet-up'
if ($KeepAlive) { $inner = $inner + '; exec sleep infinity' }
$wslArgs = '-d {0} -- bash -lc "{1}"' -f $Distro, $inner

if ($Visible) {
  $action = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument $wslArgs
} else {
  $action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument ('--headless wsl.exe ' + $wslArgs)
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

if ($existing) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Description 'agent-switchboard: start Claude Code fleet in WSL tmux' | Out-Null

Write-Host "Registered scheduled task '$TaskName':"
Write-Host ("  {0} {1}" -f $action.Execute, $action.Arguments)
Write-Host "Run it now with:  Start-ScheduledTask -TaskName $TaskName"
