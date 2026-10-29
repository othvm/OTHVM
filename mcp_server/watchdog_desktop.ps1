$ErrorActionPreference = 'SilentlyContinue'
$ok = $false
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/health' -TimeoutSec 3
    if ($h.ok) { $ok = $true }
} catch {}
if (-not $ok) {
    $launcher = Join-Path $PSScriptRoot 'start_desktop.vbs'
    Start-Process -FilePath 'wscript.exe' -ArgumentList "`"$launcher`"" -WindowStyle Hidden
}
