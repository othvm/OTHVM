# Install the Telegram fast-path and Caddy router as NSSM services.
# Run elevated (SYSTEM) because nssm writes to the service control manager.
$ErrorActionPreference = 'Continue'

$dir   = $PSScriptRoot
$root  = (Resolve-Path (Join-Path $dir '..\..')).Path
$nssm  = if ($env:NSSM_EXE) { $env:NSSM_EXE } else { (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source }
if (-not $nssm) { $nssm = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe' }
$caddy = if ($env:CADDY_EXE) { $env:CADDY_EXE } else { (Get-Command caddy.exe -ErrorAction SilentlyContinue).Source }
if (-not $caddy) { $caddy = 'C:\ProgramData\chocolatey\lib\caddy\tools\caddy.exe' }
$py    = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { Join-Path $root 'mcp_server\.venv\Scripts\python.exe' }

function Nssm {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $output = & $nssm @Arguments 2>&1
    $code = $LASTEXITCODE
    return @{ code = $code; output = ($output -join ' ') }
}

# Free the ports from any manually started instance.
foreach ($port in 5680, 5681) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
}

function Install-OthvmService {
    param([string]$Name, [string]$App, [string]$Parameters, [string]$WorkDir, [string]$LogBase)

    Nssm stop $Name | Out-Null
    Nssm remove $Name confirm | Out-Null
    $r = Nssm install $Name $App $Parameters
    Write-Output "install $Name code=$($r.code) out=$($r.output)"
    Nssm set $Name AppDirectory $WorkDir | Out-Null
    Nssm set $Name AppStdout "$LogBase.out.log" | Out-Null
    Nssm set $Name AppStderr "$LogBase.err.log" | Out-Null
    Nssm set $Name AppRotateFiles 1 | Out-Null
    Nssm set $Name AppRotateBytes 1048576 | Out-Null
    Nssm set $Name Start SERVICE_AUTO_START | Out-Null
    Nssm set $Name AppNoConsole 1 | Out-Null
    $r = Nssm start $Name
    Write-Output "start $Name code=$($r.code) out=$($r.output)"
}

Install-OthvmService -Name 'OTHVM-FastPath' -App $py -Parameters "`"$dir\main.py`"" -WorkDir $dir -LogBase "$dir\service"
Install-OthvmService -Name 'OTHVM-Caddy' -App $caddy -Parameters "run --config `"$dir\Caddyfile`" --adapter caddyfile" -WorkDir $dir -LogBase "$dir\caddy"

Start-Sleep -Seconds 4

foreach ($port in 5680, 5681) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($conn) { Write-Output "port $port listening pid=$($conn.OwningProcess)" }
    else { Write-Output "port $port NOT listening" }
}

try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:5681/health' -TimeoutSec 5
    Write-Output "fastpath health ok=$($r.ok) service=$($r.service)"
} catch { Write-Output "fastpath health ERR $($_.Exception.Message)" }

try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5680/healthz' -TimeoutSec 10
    Write-Output "caddy->n8n HTTP=$($r.StatusCode) body=$($r.Content)"
} catch { Write-Output "caddy->n8n ERR $($_.Exception.Message)" }

Write-Output "status fastpath=$((Nssm status OTHVM-FastPath).output)"
Write-Output "status caddy=$((Nssm status OTHVM-Caddy).output)"

