# One-off agent restart; not for user docs
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not (Test-Path (Join-Path $root "backend\run.py"))) {
    $root = "E:\lobster_online"
}
Set-Location $root

foreach ($port in @(8000, 8001, 18789)) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Seconds 2

Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "run_mcp.bat" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 3
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "run_backend.bat" -WorkingDirectory $root -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(50)
$ok = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $ok) {
    Write-Error "Backend did not respond on /api/health within 50s"
    exit 1
}
Write-Host "OK: backend health 200"
