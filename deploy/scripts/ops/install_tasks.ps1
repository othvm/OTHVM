# Install the OTHVM reliability tasks (MCP watchdog + weekly maintenance).
# Run from an elevated PowerShell session.
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$python = Join-Path $root 'mcp_server\.venv\Scripts\python.exe'
$watchdog = Join-Path $root 'deploy\scripts\ops\watchdog_mcp.ps1'
$maintenance = Join-Path $root 'deploy\scripts\ops\maintenance.py'

if (-not (Test-Path $python)) { throw "Missing Python virtualenv: $python" }
if (-not (Test-Path $watchdog)) { throw "Missing watchdog script: $watchdog" }
if (-not (Test-Path $maintenance)) { throw "Missing maintenance script: $maintenance" }

$watchAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdog`"" `
    -WorkingDirectory $root
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$watchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'OTHVM-MCP-Watchdog-5m' -Action $watchAction -Trigger $watchTrigger `
    -Settings $watchSettings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null

$maintAction = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$maintenance`"" -WorkingDirectory $root
$maintTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 4:00am
$maintSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'OTHVM-Weekly-Maintenance' -Action $maintAction -Trigger $maintTrigger `
    -Settings $maintSettings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null

Get-ScheduledTask -TaskName 'OTHVM-MCP-Watchdog-5m', 'OTHVM-Weekly-Maintenance' |
    Select-Object TaskName, State | Format-Table -AutoSize
