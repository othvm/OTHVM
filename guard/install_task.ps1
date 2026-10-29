# Register the OTHVM guard as a SYSTEM scheduled task. Run from an elevated shell.
$ErrorActionPreference = 'Stop'

$name = 'OTHVM Guard'
$script = Join-Path $PSScriptRoot 'run_guard.cmd'
if (-not (Test-Path $script)) { throw "Missing $script" }

$action = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument "/c `"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 3) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($task) { Write-Output "registered $name state=$($task.State)" } else { throw 'registration failed' }

Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 10
$info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
Write-Output "lastRun=$($info.LastRunTime) lastResult=$($info.LastTaskResult)"
