# OTHVM MCP watchdog — runs as SYSTEM every 5 minutes from Task Scheduler.
# Restarts the service when /health fails or the heartbeat file goes stale.
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$log = Join-Path $root 'mcp_server\logs\watchdog.log'
$heartbeat = Join-Path $root 'mcp_server\heartbeat.json'
$url = 'http://127.0.0.1:8765/health'
$nssm = if ($env:NSSM_EXE) { $env:NSSM_EXE } else { (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source }
if (-not $nssm) { $nssm = 'C:\ProgramData\chocolatey\lib\NSSM\tools\nssm.exe' }

function Write-Log([string]$msg) {
    $line = "$(Get-Date -Format o) $msg"
    try {
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2000000)) {
            Move-Item -Force $log "$log.1"
        }
        Add-Content -Path $log -Value $line -Encoding utf8
    } catch {}
}

$healthy = $false
$detail = ''
try {
    $r = Invoke-RestMethod -Uri $url -TimeoutSec 8
    $healthy = [bool]$r.ok
    $detail = "health ok=$($r.ok) pid=$($r.pid) loops=$($r.loops_worker) uptime=$($r.uptime_seconds)"
} catch {
    $detail = "health error: $($_.Exception.Message)"
}

$stale = $false
if (Test-Path $heartbeat) {
    try {
        $hb = Get-Content $heartbeat -Raw | ConvertFrom-Json
        $age = ((Get-Date).ToUniversalTime() - ([datetime]$hb.time).ToUniversalTime()).TotalSeconds
        if ($age -gt 180) { $stale = $true; $detail += " heartbeat_age=${age}s" }
    } catch {
        $stale = $true
        $detail += ' heartbeat unreadable'
    }
} else {
    $stale = $true
    $detail += ' heartbeat missing'
}

if (-not $healthy -or $stale) {
    $service = Get-Service -Name 'OTHVM-MCP' -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Log "unhealthy but service OTHVM-MCP is not installed: $detail"
        exit 0
    }
    Write-Log "restarting OTHVM-MCP: $detail"
    & $nssm restart OTHVM-MCP | Out-Null
    Start-Sleep -Seconds 6
    try {
        $r2 = Invoke-RestMethod -Uri $url -TimeoutSec 10
        Write-Log "restart result: ok=$($r2.ok) pid=$($r2.pid)"
    } catch {
        Write-Log "restart verification failed: $($_.Exception.Message)"
    }
}
