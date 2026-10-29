# Install the Android agent as an NSSM service. Run from an elevated shell.
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$app = $PSScriptRoot
$python = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { Join-Path $root 'mcp_server\.venv\Scripts\python.exe' }
$nssm = if ($env:NSSM_EXE) { $env:NSSM_EXE } else { (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source }
if (-not $nssm) { $nssm = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe' }

if (-not (Test-Path $python)) { throw "Missing Python interpreter: $python" }
if (-not (Test-Path (Join-Path $app 'main.py'))) { throw 'Missing android_agent/main.py' }

& $nssm stop OTHVM-AndroidAgent 2>$null | Out-Null
& $nssm remove OTHVM-AndroidAgent confirm 2>$null | Out-Null

& $nssm install OTHVM-AndroidAgent $python (Join-Path $app 'main.py')
& $nssm set OTHVM-AndroidAgent AppDirectory $app
& $nssm set OTHVM-AndroidAgent DisplayName 'OTHVM Android Agent'
& $nssm set OTHVM-AndroidAgent Description 'Local ADB bridge for the OTHVM Android tool (port 8767).'
& $nssm set OTHVM-AndroidAgent Start SERVICE_AUTO_START
& $nssm set OTHVM-AndroidAgent ObjectName LocalSystem
& $nssm set OTHVM-AndroidAgent AppStdout (Join-Path $app 'logs\service.out.log')
& $nssm set OTHVM-AndroidAgent AppStderr (Join-Path $app 'logs\service.err.log')
& $nssm set OTHVM-AndroidAgent AppRotateFiles 1
& $nssm set OTHVM-AndroidAgent AppRotateOnline 1
& $nssm set OTHVM-AndroidAgent AppRotateBytes 10485760
& $nssm set OTHVM-AndroidAgent AppExit Default Restart
& $nssm set OTHVM-AndroidAgent AppRestartDelay 5000
& $nssm set OTHVM-AndroidAgent AppThrottle 3000

New-Item -ItemType Directory -Force -Path (Join-Path $app 'logs') | Out-Null
& $nssm start OTHVM-AndroidAgent
Start-Sleep -Seconds 4
Get-Service -Name OTHVM-AndroidAgent | Select-Object Name, Status, StartType | Format-Table -AutoSize
